"""SQLite schema and the flush transaction.

Four tables. No raw events, no identity columns, no retention machinery.

The load-bearing rule lives in `flush_windows`:

    persisted_cursor = greatest time_us whose contribution is durably
                       represented in the SAME transaction

Buckets, window health, and the resume cursor commit together or not at all.
The cursor can therefore never point past data that was not written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # avoids a circular import at runtime
    from .accumulator import ClosedWindow

SCHEMA_VERSION = "1"

#: Kept local to the collector, deliberately. SQLite over NFS/SMB corrupts.
DEFAULT_DB_PATH = Path("data/weatherwatch.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per observation session. Outside a run, time is NOT OBSERVED --
-- which is different from observed-and-empty, and different again from a
-- gap inside a run. The whole point of this table is keeping those apart.
CREATE TABLE IF NOT EXISTS observation_run (
    run_id                 TEXT PRIMARY KEY,
    source_endpoint        TEXT    NOT NULL,
    collector_version      TEXT    NOT NULL,
    bucket_width           INTEGER NOT NULL,
    started_at             TEXT    NOT NULL,
    planned_end            TEXT,
    ended_at               TEXT,
    stop_reason            TEXT,
    resume_cursor_at_start INTEGER,
    first_event_us         INTEGER,
    last_event_us          INTEGER
);

-- The product. `run_id` is in the primary key because two observation runs
-- are NOT additive by default: they may come from different endpoints or
-- overlap in time. Attribution is explicit so a query cannot sum them by
-- accident. See read.py:assert_summable.
CREATE TABLE IF NOT EXISTS bucket (
    run_id       TEXT    NOT NULL,
    bucket_start INTEGER NOT NULL,
    bucket_width INTEGER NOT NULL,
    metric       TEXT    NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (run_id, bucket_start, bucket_width, metric)
) WITHOUT ROWID;

-- The honesty ledger. Written for every closed window inside a run,
-- including empty ones: a present row with events_seen=0 means "we watched
-- and nothing happened"; an absent row means "we were not watching."
CREATE TABLE IF NOT EXISTS window_health (
    run_id               TEXT    NOT NULL,
    bucket_start         INTEGER NOT NULL,
    bucket_width         INTEGER NOT NULL,
    events_seen          INTEGER NOT NULL,
    parse_errors         INTEGER NOT NULL,
    unclassified         INTEGER NOT NULL,
    rejected_no_time_us  INTEGER NOT NULL,
    late_events          INTEGER NOT NULL,
    lag_ewma_s           REAL,
    lag_max_s            REAL,
    reconnects           INTEGER NOT NULL,
    gap_us               INTEGER NOT NULL,
    resume_seam          INTEGER NOT NULL,
    observed_from_us     INTEGER,
    observed_to_us       INTEGER,
    observed_duration_us INTEGER NOT NULL,
    coverage_state       TEXT    NOT NULL,
    gate_reasons         TEXT,
    partial              INTEGER NOT NULL,
    PRIMARY KEY (run_id, bucket_start, bucket_width)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_bucket_time   ON bucket(bucket_start, metric);
CREATE INDEX IF NOT EXISTS idx_run_endpoint  ON observation_run(source_endpoint, started_at);
"""


def cursor_key(endpoint: str) -> str:
    """Resume cursors are per-endpoint.

    M0 falsified the assumption that Jetstream instances are interchangeable
    views, so a cursor from one relay is meaningless against another. Keying
    by endpoint makes cross-relay resume impossible by construction rather
    than by discipline.
    """
    return f"cursor:{endpoint}"


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_size_limit=67108864")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    set_meta(conn, "schema_version", SCHEMA_VERSION)


# --- meta ------------------------------------------------------------------

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_cursor(conn: sqlite3.Connection, endpoint: str) -> int | None:
    raw = get_meta(conn, cursor_key(endpoint))
    return int(raw) if raw is not None else None


# --- runs ------------------------------------------------------------------

def start_run(
    conn: sqlite3.Connection,
    run_id: str,
    endpoint: str,
    collector_version: str,
    bucket_width: int,
    started_at: str,
    planned_end: str | None,
    resume_cursor: int | None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO observation_run "
            "(run_id, source_endpoint, collector_version, bucket_width, "
            " started_at, planned_end, resume_cursor_at_start) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, endpoint, collector_version, bucket_width,
             started_at, planned_end, resume_cursor),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def end_run(
    conn: sqlite3.Connection,
    run_id: str,
    ended_at: str,
    stop_reason: str,
    first_event_us: int | None,
    last_event_us: int | None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE observation_run SET ended_at=?, stop_reason=?, "
            "first_event_us=?, last_event_us=? WHERE run_id=?",
            (ended_at, stop_reason, first_event_us, last_event_us, run_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM observation_run WHERE run_id=?", (run_id,)
    ).fetchone()


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM observation_run ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


# --- the flush transaction -------------------------------------------------

class FlushIntegrityError(RuntimeError):
    """A window was flushed twice within one run. That is a bug, not
    contention: windows close in stream order and each closes once. Crashing
    loud beats silently double-counting."""


def flush_windows(
    conn: sqlite3.Connection,
    run_id: str,
    endpoint: str,
    windows: Sequence["ClosedWindow"],
    cursor_time_us: int | None,
) -> None:
    """Atomically persist closed windows and advance the resume cursor.

    `cursor_time_us` MUST be the greatest `time_us` whose contribution is in
    `windows`. Passing anything larger breaks the commit invariant and would
    let a restart skip uncommitted events.

    An empty `windows` list with a non-None cursor is refused for the same
    reason: there is no committed contribution for the cursor to describe.
    """
    if not windows:
        if cursor_time_us is not None:
            raise ValueError("refusing to advance cursor with no committed windows")
        return

    max_seen = max((w.last_event_us for w in windows if w.last_event_us), default=None)
    if cursor_time_us is not None and max_seen is not None and cursor_time_us > max_seen:
        raise ValueError(
            f"cursor {cursor_time_us} exceeds greatest committed time_us {max_seen}"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        for w in windows:
            for metric, count in sorted(w.counts.items()):
                conn.execute(
                    "INSERT INTO bucket(run_id, bucket_start, bucket_width, metric, count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, w.bucket_start, w.bucket_width, metric, count),
                )
            conn.execute(
                "INSERT INTO window_health("
                " run_id, bucket_start, bucket_width, events_seen, parse_errors,"
                " unclassified, rejected_no_time_us, late_events, lag_ewma_s,"
                " lag_max_s, reconnects, gap_us, resume_seam, observed_from_us,"
                " observed_to_us, observed_duration_us, coverage_state,"
                " gate_reasons, partial) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, w.bucket_start, w.bucket_width, w.events_seen,
                    w.parse_errors, w.unclassified, w.rejected_no_time_us,
                    w.late_events, w.lag_ewma_s, w.lag_max_s, w.reconnects,
                    w.gap_us, int(w.resume_seam), w.first_event_us,
                    w.last_event_us, w.observed_duration_us, w.coverage_state,
                    ",".join(w.gate_reasons) if w.gate_reasons else None,
                    int(w.partial),
                ),
            )
        if cursor_time_us is not None:
            set_meta(conn, cursor_key(endpoint), str(cursor_time_us))
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise FlushIntegrityError(
            f"window already flushed for run {run_id}: {exc}"
        ) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
