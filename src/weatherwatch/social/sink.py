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
    connect,
    init_db,
)

LOG = logging.getLogger("weatherwatch.social.sink")


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

    def close(self, now_us: int) -> None:
        self.flush(now_us)
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    @property
    def errors(self) -> int:
        return self._errors

    def health_snapshot(self) -> dict:
        snap = self.writer.health_snapshot()
        snap["sink_errors"] = self._errors
        return snap
