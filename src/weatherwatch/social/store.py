"""Edge custody — a separate database file with its own retention posture.

Separate from the weather database on purpose. The weather DB's guarantee is
"no people in here"; that guarantee has a deployed page making public claims
on it, and it stays true because this sink never writes to that file. Two
postures, two files, one collector.

VOLUME, STATED UP FRONT
-----------------------
The deployed weather instrument measured, as observed at one endpoint:
likes ~216/s, reposts ~34/s, follows ~26/s, blocks ~5/s. Retaining every
tracked edge is therefore on the order of 24M rows/day. That is why:

* the sink is **off by default** and must be turned on explicitly;
* `--collections` narrows what is retained;
* a retention horizon prunes on flush, defaulting to 3 days.

Do not switch this on inside the deployed weather collector without deciding
those three things deliberately.

WRITER DISCIPLINE
-----------------
Batched, one transaction per flush. Ported as doctrine from
`driftwatch/docs/JETSTREAM_INGEST_REALITIES.md`: naive per-event ingest sheds
30-40% of the stream while `/health` still reports ok. The counter that makes
that failure visible here is `sink_health.dropped_backpressure` — a buffer
overrun is recorded, never silently absorbed.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import SOCIAL_SCHEMA_VERSION
from .edges import SKIP_REASONS, EdgeEvent, StatusEvent
from .envelope import DetectionEnvelope, envelope_to_dict, receipt_hash, stable_json

DEFAULT_EDGE_DB_PATH = Path("data/social.sqlite3")

#: Hard cap on rows buffered before a forced flush. Beyond this the sink drops
#: and *counts* rather than growing without bound.
MAX_BUFFER_ROWS = 20_000

#: Flush at least this often, even with the buffer nowhere near full.
#:
#: Row count alone is the wrong trigger on a narrow collection set. Observed on
#: the deployed host: with `block,listitem` retained (~5/s), a 2,000-row batch
#: takes ~7 minutes to fill, so a SIGKILL would drop seven minutes of custody
#: while the weather lane -- which commits every closed 60s window -- would
#: lose at most one. Two lanes reading the same socket should not have
#: durability windows an order of magnitude apart.
DEFAULT_FLUSH_INTERVAL_S = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per observed record operation. There is no text column, no handle
-- column, and no profile column, because there is no such thing to put in
-- one. That is the retention guarantee, expressed as a schema rather than as
-- a promise. `tests/social/test_boundaries.py` asserts it.
CREATE TABLE IF NOT EXISTS edge_event (
    observed_us       INTEGER NOT NULL,
    actor_did         TEXT    NOT NULL,
    collection        TEXT    NOT NULL,
    op                TEXT    NOT NULL,
    subject_kind      TEXT    NOT NULL,
    subject_ref       TEXT    NOT NULL,
    rkey              TEXT    NOT NULL,
    rev               TEXT    NOT NULL,
    cid               TEXT    NOT NULL,
    record_created_at TEXT    NOT NULL,
    PRIMARY KEY (actor_did, collection, rkey, op, observed_us)
) WITHOUT ROWID;

-- Account lifecycle transitions. Retained because withdrawal is an event:
-- an instrument that only records creation cannot see anyone leave.
CREATE TABLE IF NOT EXISTS status_event (
    observed_us INTEGER NOT NULL,
    actor_did   TEXT    NOT NULL,
    active      INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    PRIMARY KEY (actor_did, observed_us, status)
) WITHOUT ROWID;

-- The honesty ledger for this sink, mirroring the weather lane's.
CREATE TABLE IF NOT EXISTS sink_health (
    run_id               TEXT    NOT NULL,
    flush_seq            INTEGER NOT NULL,
    flushed_at_us        INTEGER NOT NULL,
    seen                 INTEGER NOT NULL,
    stored_edges         INTEGER NOT NULL,
    stored_status        INTEGER NOT NULL,
    dropped_backpressure INTEGER NOT NULL,
    skips_json           TEXT    NOT NULL,
    first_event_us       INTEGER,
    last_event_us        INTEGER,
    PRIMARY KEY (run_id, flush_seq)
) WITHOUT ROWID;

-- Sealed DetectionEnvelopes. `det_id` is the envelope's own identity; the
-- subject is always an episode, never an account.
CREATE TABLE IF NOT EXISTS episode (
    det_id             TEXT PRIMARY KEY,
    detector_id        TEXT NOT NULL,
    detector_version   TEXT NOT NULL,
    type               TEXT NOT NULL,
    ts_start           TEXT NOT NULL,
    ts_end             TEXT NOT NULL,
    window             TEXT NOT NULL,
    subject_type       TEXT NOT NULL,
    subject_value      TEXT NOT NULL,
    score              REAL NOT NULL,
    severity           TEXT NOT NULL,
    evidence_id        TEXT NOT NULL,
    config_hash        TEXT NOT NULL,
    window_fingerprint TEXT NOT NULL,
    receipt_hash       TEXT NOT NULL,
    envelope_json      TEXT NOT NULL,
    sealed_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edge_time    ON edge_event(observed_us, collection);
CREATE INDEX IF NOT EXISTS idx_edge_subject ON edge_event(subject_ref, observed_us);
CREATE INDEX IF NOT EXISTS idx_status_time  ON status_event(observed_us);
CREATE INDEX IF NOT EXISTS idx_episode_time ON episode(ts_start, type);
CREATE INDEX IF NOT EXISTS idx_episode_ev   ON episode(evidence_id);
"""

#: Every table this database may contain. A per-actor rollup table would be a
#: dossier by another name, so the allowlist is asserted by test.
ALLOWED_TABLES = frozenset({
    "meta", "edge_event", "status_event", "sink_health", "episode",
})

#: Column-name substrings that would mean content or profile retention had
#: crept in. Asserted by test against the live schema.
FORBIDDEN_COLUMN_SUBSTRINGS = (
    "text", "content", "handle", "display", "descr", "avatar", "bio",
    "profile", "score_total", "reputation",
)


def connect(path: Path | str = DEFAULT_EDGE_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    set_meta(conn, "social_schema_version", str(SOCIAL_SCHEMA_VERSION))
    if get_meta(conn, "token_salt") is None:
        # Display tokens are salted per store. Unsalted, a DID hash is
        # reversible by anyone willing to enumerate the DID space, which is
        # public — so an unsalted token would be pseudonymity theatre. The
        # salt never leaves this file, and reports built from two different
        # stores are therefore not joinable by token.
        set_meta(conn, "token_salt", secrets.token_hex(16))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def token_salt(conn: sqlite3.Connection) -> str:
    salt = get_meta(conn, "token_salt")
    if salt is None:
        raise RuntimeError("store not initialised: no token_salt")
    return salt


def actor_token(salt: str, did: str) -> str:
    """Opaque, store-local display handle for an actor.

    NOT anonymisation. It keeps identity labels off the envelope and out of
    the report while leaving counts joinable *within* one store. Anyone
    holding this store's salt and a DID can recompute it.
    """
    return "a:" + receipt_hash({"salt": salt, "did": did})[:12]


def event_receipt(e: EdgeEvent) -> str:
    """Content-addressed commitment to one observed edge.

    Unsalted, unlike display tokens: a receipt's job is to commit to the exact
    record observed so a later replay can prove the evidence did not change.
    """
    return receipt_hash({
        "actor": e.actor_did, "collection": e.collection, "op": e.op,
        "rkey": e.rkey, "rev": e.rev, "cid": e.cid,
        "subject_kind": e.subject_kind, "subject_ref": e.subject_ref,
        "observed_us": e.observed_us,
    })


@dataclass
class FlushStats:
    seen: int = 0
    stored_edges: int = 0
    stored_status: int = 0
    dropped_backpressure: int = 0


class EdgeWriter:
    """Buffered writer. One transaction per flush, drop accounting on overrun."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        batch_rows: int = 2_000,
        retention_us: int | None = None,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ):
        self.conn = conn
        self.run_id = run_id
        self.batch_rows = batch_rows
        self.retention_us = retention_us
        self.flush_interval_us = int(flush_interval_s * 1_000_000)
        self._last_flush_us: int | None = None
        self._edges: list[EdgeEvent] = []
        self._status: list[StatusEvent] = []
        self._skips: dict[str, int] = {r: 0 for r in SKIP_REASONS}
        self._stats = FlushStats()
        self._flush_seq = 0
        self._first_us: int | None = None
        self._last_us: int | None = None

    # -- intake ------------------------------------------------------------

    def note_skip(self, reason: str) -> None:
        self._stats.seen += 1
        self._skips[reason] = self._skips.get(reason, 0) + 1

    def add_edge(self, e: EdgeEvent) -> None:
        self._stats.seen += 1
        if len(self._edges) + len(self._status) >= MAX_BUFFER_ROWS:
            self._stats.dropped_backpressure += 1
            return
        self._edges.append(e)
        self._track_time(e.observed_us)

    def add_status(self, s: StatusEvent) -> None:
        self._stats.seen += 1
        if len(self._edges) + len(self._status) >= MAX_BUFFER_ROWS:
            self._stats.dropped_backpressure += 1
            return
        self._status.append(s)
        self._track_time(s.observed_us)

    def _track_time(self, us: int) -> None:
        if self._first_us is None or us < self._first_us:
            self._first_us = us
        if self._last_us is None or us > self._last_us:
            self._last_us = us

    @property
    def pending(self) -> int:
        return len(self._edges) + len(self._status)

    def should_flush(self, now_us: int | None = None) -> bool:
        """Full buffer, or an old one. Never flushes an empty buffer."""
        if self.pending >= self.batch_rows:
            return True
        if not self.pending or now_us is None:
            return False
        if self._last_flush_us is None:
            self._last_flush_us = now_us
            return False
        return (now_us - self._last_flush_us) >= self.flush_interval_us

    # -- output ------------------------------------------------------------

    def flush(self, now_us: int) -> int:
        """Commit buffered rows plus a health row. Returns rows written."""
        edges, status = self._edges, self._status
        self._edges, self._status = [], []
        self._last_flush_us = now_us
        written = 0

        self.conn.execute("BEGIN")
        try:
            if edges:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO edge_event(observed_us, actor_did, "
                    "collection, op, subject_kind, subject_ref, rkey, rev, cid, "
                    "record_created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(e.observed_us, e.actor_did, e.collection, e.op,
                      e.subject_kind, e.subject_ref, e.rkey, e.rev, e.cid,
                      e.record_created_at) for e in edges],
                )
                written += len(edges)
                self._stats.stored_edges += len(edges)
            if status:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO status_event(observed_us, actor_did, "
                    "active, status) VALUES(?,?,?,?)",
                    [(s.observed_us, s.actor_did, s.active, s.status)
                     for s in status],
                )
                written += len(status)
                self._stats.stored_status += len(status)

            self._flush_seq += 1
            self.conn.execute(
                "INSERT OR REPLACE INTO sink_health(run_id, flush_seq, "
                "flushed_at_us, seen, stored_edges, stored_status, "
                "dropped_backpressure, skips_json, first_event_us, "
                "last_event_us) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, self._flush_seq, now_us, self._stats.seen,
                 self._stats.stored_edges, self._stats.stored_status,
                 self._stats.dropped_backpressure, stable_json(self._skips),
                 self._first_us, self._last_us),
            )

            if self.retention_us is not None and self._last_us is not None:
                horizon = self._last_us - self.retention_us
                self.conn.execute(
                    "DELETE FROM edge_event WHERE observed_us < ?", (horizon,))
                self.conn.execute(
                    "DELETE FROM status_event WHERE observed_us < ?", (horizon,))

            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return written

    def health_snapshot(self) -> dict:
        return {
            "seen": self._stats.seen,
            "stored_edges": self._stats.stored_edges,
            "stored_status": self._stats.stored_status,
            "dropped_backpressure": self._stats.dropped_backpressure,
            "skips": dict(self._skips),
            "pending": self.pending,
        }


# -- episode persistence ----------------------------------------------------

def save_episode(
    conn: sqlite3.Connection, env: DetectionEnvelope, evidence_id: str,
    sealed_at: str,
) -> None:
    d = envelope_to_dict(env)
    conn.execute(
        "INSERT OR REPLACE INTO episode(det_id, detector_id, detector_version, "
        "type, ts_start, ts_end, window, subject_type, subject_value, score, "
        "severity, evidence_id, config_hash, window_fingerprint, receipt_hash, "
        "envelope_json, sealed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (env.det_id, env.detector_id, env.detector_version, env.type,
         env.ts_start, env.ts_end, env.window, env.subject.type,
         env.subject.value, env.score, env.severity, evidence_id,
         env.config_hash, env.window_fingerprint, env.receipt_hash,
         stable_json(d), sealed_at),
    )


def list_episodes(
    conn: sqlite3.Connection, since: str | None = None, until: str | None = None,
    limit: int = 5_000,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM episode"
    clauses, params = [], []
    if since:
        clauses.append("ts_end >= ?")
        params.append(since)
    if until:
        clauses.append("ts_start <= ?")
        params.append(until)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts_start LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
