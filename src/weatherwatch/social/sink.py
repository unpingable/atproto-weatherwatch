"""The collector hook. Twelve lines of behaviour and a lot of care about where.

The weather lane parses a message and hands it to `classify()`. That parse is
the only work worth sharing, so this sink attaches to the same parsed dict and
nothing else. It does not open a socket, does not hold a cursor, does not
decide when to reconnect, and cannot affect what the weather lane stores.

Failure posture: an exception in this sink must never take down the
observation it is riding on. `observe()` swallows and counts, because a
collector that dies on a sensor bug loses stream time it can never get back —
and stream time is the one thing this estate cannot re-derive.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .edges import SKIP_MALFORMED, EdgeEvent, Skipped, StatusEvent, extract
from .store import (
    DEFAULT_FLUSH_INTERVAL_S,
    EdgeWriter,
    JOURNAL_SIZE_LIMIT_BYTES,
    checkpoint,
    close as close_store,
    connect,
    init_db,
    wal_bytes,
)

LOG = logging.getLogger("weatherwatch.social.sink")

#: Below this the collector leaves the WAL to SQLite's own auto-checkpoint.
#: The same size `journal_size_limit` trims a restarted log back to.
FLUSH_CHECKPOINT_WAL_BYTES = JOURNAL_SIZE_LIMIT_BYTES


class SocialSink:
    """Optional second sink on the collector's parsed-message path."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        collections: frozenset[str] | None = None,
        batch_rows: int = 2_000,
        retention_us: int | None = None,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ):
        self.conn = conn
        self.collections = collections
        self.writer = EdgeWriter(
            conn, run_id, batch_rows=batch_rows, retention_us=retention_us,
            flush_interval_s=flush_interval_s,
        )
        self._errors = 0

    @classmethod
    def open(
        cls, path: Path | str, run_id: str, collections: frozenset[str] | None = None,
        batch_rows: int = 2_000, retention_us: int | None = None,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> "SocialSink":
        conn = connect(path)
        init_db(conn)
        return cls(conn, run_id, collections, batch_rows, retention_us,
                   flush_interval_s)

    def observe(self, msg: object) -> None:
        try:
            ev = extract(msg)
        except Exception:
            self._errors += 1
            self.writer.note_skip(SKIP_MALFORMED)
            return

        if isinstance(ev, Skipped):
            self.writer.note_skip(ev.reason)
            return
        if isinstance(ev, StatusEvent):
            self.writer.add_status(ev)
            return
        if isinstance(ev, EdgeEvent):
            if self.collections is not None and ev.collection not in self.collections:
                # Narrowed by operator choice, not by observer failure. Counted
                # under its own reason so the two never merge -- the same
                # distinction weatherwatch's CANDIDATES.md C1 is about.
                self.writer.note_skip("untracked_collection")
                return
            self.writer.add_edge(ev)

    def maybe_flush(self, now_us: int) -> None:
        if self.writer.should_flush(now_us):
            self.flush(now_us)

    def flush(self, now_us: int) -> None:
        try:
            self.writer.flush(now_us)
        except sqlite3.Error:
            LOG.exception("social sink flush failed; weather lane unaffected")
            self._errors += 1
            return
        # This runs on the collector's ingest path. Truncation belongs to the
        # batch jobs' close path; here, only when the log has grown past the
        # size SQLite would trim it to (e.g. a field run's frames left behind
        # by a close that had to settle for PASSIVE), copy frames back
        # PASSIVE: no writer lock, no wait, no truncate.
        if wal_bytes(self.conn) > FLUSH_CHECKPOINT_WAL_BYTES:
            checkpoint(self.conn, busy_timeout_ms=0, passive_only=True)

    def close(self, now_us: int) -> None:
        self.flush(now_us)
        close_store(self.conn)

    @property
    def errors(self) -> int:
        return self._errors

    def health_snapshot(self) -> dict:
        snap = self.writer.health_snapshot()
        snap["sink_errors"] = self._errors
        return snap
