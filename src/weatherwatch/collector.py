"""M1 — the collector process.

One asyncio loop, one SQLite connection, one writer. No bounded queue, no
writer thread, no thread pool, no resolver, no outbound calls. At M0's
measured ~330 eps the per-event work is a dict lookup and an integer
increment, so the decoupling machinery driftwatch needed for its 3-connects-
and-4-commits-per-event hot path has no problem to solve here.

Reconnect discipline
--------------------
A reconnect is treated exactly like a restart with respect to *uncommitted*
state:

    1. flush any fully-closed windows (advancing the durable cursor)
    2. discard the in-flight window's counts — they were never committed
    3. reconnect at persisted_cursor + 1 and reconstruct that interval

M0 established `cursor=T` is inclusive and `cursor=T+1` resumes exactly, so
step 3 replays precisely the discarded interval: no overlap, no gap, no
identity-based deduplication. Using one path for reconnect and restart means
there is only one correctness argument to make.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import secrets
import sqlite3
import time
from pathlib import Path

import websockets

from . import COLLECTOR_VERSION, db, timeutil
from .accumulator import DEFAULT_BUCKET_WIDTH_S, Accumulator
from .classify import classify
from .health import ObservationHealth

LOG = logging.getLogger("weatherwatch.collector")

#: The three public relays M0 measured. Named so an endpoint change is an
#: explicit act rather than a typo.
KNOWN_ENDPOINTS = {
    "jetstream1.us-east": "wss://jetstream1.us-east.bsky.network/subscribe",
    "jetstream1.us-west": "wss://jetstream1.us-west.bsky.network/subscribe",
    "jetstream2.us-east": "wss://jetstream2.us-east.bsky.network/subscribe",
}

#: M0 measured jetstream1.us-east carrying the highest post volume
#: (7,088 / 120s vs 4,400 on jetstream2.us-east, with two sockets to
#: jetstream2 agreeing exactly). Defaulting here follows the M0 brief.
#:
#: Higher volume is NOT evidence of greater completeness. M0 could not
#: establish set inclusion — aggregate counts cannot, and proving it would
#: need per-event identity, which this project refuses to retain. Every
#: figure this collector produces is "as observed at this endpoint."
DEFAULT_ENDPOINT = KNOWN_ENDPOINTS["jetstream1.us-east"]

RECONNECT_BACKOFF_START_S = 1.0
RECONNECT_BACKOFF_CAP_S = 30.0
#: Beyond this, a resume is assumed to have fallen outside the relay's replay
#: horizon (M0 verified >=1h exact, >=6h unresponsive) and the discontinuity
#: is recorded as a gap rather than quietly absorbed.
GAP_ALERT_US = 2_000_000


def new_run_id() -> str:
    """Opaque, sortable, and says nothing about the machine or the user."""
    stamp = timeutil.now_utc().strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(3)}"


def resolve_endpoint(value: str | None) -> str:
    if not value:
        return DEFAULT_ENDPOINT
    if value in KNOWN_ENDPOINTS:
        return KNOWN_ENDPOINTS[value]
    return value


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        endpoint: str,
        duration_s: float | None = None,
        bucket_width: int = DEFAULT_BUCKET_WIDTH_S,
        checkpoint_path: Path | None = None,
        social_sink=None,
    ):
        self.conn = conn
        self.endpoint = endpoint
        self.duration_s = duration_s
        self.bucket_width = bucket_width
        self.checkpoint_path = checkpoint_path
        #: Optional second sink on the parsed-message path (weatherwatch.social).
        #: None in every code default. A deployment may explicitly enable the
        #: separate bounded-custody lane; doing so never changes the weather
        #: database or classifier output. See social/BOUNDARIES.md.
        self.social_sink = social_sink

        self.run_id = new_run_id()
        self.health = ObservationHealth()
        self.acc = Accumulator(self.run_id, bucket_width=bucket_width)

        self._stop = False
        self._stop_reason = "completed"
        self._reconnects = 0
        self._messages = 0
        self._resume_from_us: int | None = None
        self._awaiting_first_event_after_resume = False
        self._started_mono = 0.0

    # -- lifecycle ---------------------------------------------------------

    def _start_run(self) -> int | None:
        resume_cursor = db.get_cursor(self.conn, self.endpoint)
        planned_end = None
        if self.duration_s:
            planned_end = timeutil.us_to_iso(
                timeutil.now_us() + int(self.duration_s * 1_000_000)
            )
        db.start_run(
            self.conn,
            run_id=self.run_id,
            endpoint=self.endpoint,
            collector_version=COLLECTOR_VERSION,
            bucket_width=self.bucket_width,
            started_at=timeutil.now_iso(),
            planned_end=planned_end,
            resume_cursor=resume_cursor,
        )
        LOG.info(
            "run %s started endpoint=%s resume_cursor=%s width=%ds",
            self.run_id, self.endpoint,
            resume_cursor if resume_cursor is not None else "cold",
            self.bucket_width,
        )
        return resume_cursor

    def _end_run(self) -> None:
        db.end_run(
            self.conn,
            run_id=self.run_id,
            ended_at=timeutil.now_iso(),
            stop_reason=self._stop_reason,
            first_event_us=self.acc.first_event_us,
            last_event_us=self.acc.last_event_us,
        )
        LOG.info("run %s ended reason=%s", self.run_id, self._stop_reason)

    def stop(self, reason: str = "signal") -> None:
        self._stop = True
        self._stop_reason = reason

    # -- connection --------------------------------------------------------

    def _build_url(self) -> str:
        """Unfiltered subscription. M0 verified that omitting
        `wantedCollections` yields a true superset (post-count ratio 0.999
        against a filtered control), so filtering would only narrow what we
        can count.
        """
        cursor = db.get_cursor(self.conn, self.endpoint)
        if cursor is None:
            self._resume_from_us = None
            return self.endpoint
        resume = cursor + 1  # M0: cursor=T is inclusive; T+1 is exact
        self._resume_from_us = resume
        return f"{self.endpoint}?cursor={resume}"

    # -- flushing ----------------------------------------------------------

    def _stamp_health(self, w) -> None:
        losses = {
            "parse_errors": w.parse_errors,
            "rejected_no_time_us": w.rejected_no_time_us,
            "late_events": w.late_events,
            "gap_us": w.gap_us,
            "unclassified": w.unclassified,
        }
        observed_s = w.observed_duration_us / 1_000_000
        if observed_s <= 0:
            observed_s = float(w.bucket_width)
        snap = self.health.record_window(w.events_seen, observed_s, losses)
        w.coverage_state = snap["coverage_state"]
        w.gate_reasons = tuple(snap["gate_reasons"])
        w.lag_ewma_s = snap["stream_lag_s"]
        w.lag_max_s = snap["lag_max_s"]
        return snap

    def _flush_pending(self) -> None:
        closed = self.acc.take_closed()
        if not closed:
            return
        snap = None
        for w in closed:
            snap = self._stamp_health(w)
        cursor = Accumulator.commit_cursor_for(closed)
        db.flush_windows(self.conn, self.run_id, self.endpoint, closed, cursor)
        if self.social_sink is not None:
            try:
                self.social_sink.maybe_flush(timeutil.now_us())
            except Exception:
                LOG.exception("social sink flush raised; weather lane continues")
        for w in closed:
            self._log_stats(w, snap)
        if self.checkpoint_path:
            self.health.write_checkpoint(self.checkpoint_path)

    def _log_stats(self, w, snap) -> None:
        LOG.info(
            "STATS run=%s win=%d width=%ds events=%d eps=%.1f "
            "observed=%.1fs/%ds partial=%d parse_err=%d unclass=%d "
            "rejected=%d late=%d reconnects=%d gap=%.2fs seam=%d "
            "lag=%.3fs cov=%s%s",
            self.run_id, w.bucket_start, w.bucket_width, w.events_seen,
            w.events_seen / max(w.observed_duration_us / 1_000_000, 0.001),
            w.observed_duration_us / 1_000_000, w.bucket_width,
            int(w.partial), w.parse_errors, w.unclassified,
            w.rejected_no_time_us, w.late_events, w.reconnects,
            w.gap_us / 1_000_000, int(w.resume_seam),
            w.lag_ewma_s if w.lag_ewma_s is not None else -1,
            w.coverage_state,
            f" reasons={','.join(w.gate_reasons)}" if w.gate_reasons else "",
        )

    # -- message handling --------------------------------------------------

    def _handle_raw(self, raw: str) -> None:
        self._messages += 1
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.acc.note_parse_error()
            return

        # The fork. Both lanes read the same parsed message and nothing else:
        # one keeps counts, the other keeps edges. The sink is isolated -- it
        # cannot raise into this path and cannot touch the weather database.
        if self.social_sink is not None:
            try:
                self.social_sink.observe(msg)
            except Exception:
                # Stream time is the one thing this estate cannot re-derive,
                # so a sensor bug must never cost an observation. Counted by
                # the sink's own health row, not swallowed silently.
                LOG.exception("social sink raised; weather lane continues")

        c = classify(msg)
        if c is None:
            self.acc.note_rejected_no_time_us()
            return

        if self._awaiting_first_event_after_resume:
            self._awaiting_first_event_after_resume = False
            if self._resume_from_us is not None:
                gap = max(0, c.time_us - self._resume_from_us)
                if gap > GAP_ALERT_US:
                    LOG.warning(
                        "resume landed %.1fs past the requested cursor — "
                        "treating as a gap, not a clean seam", gap / 1_000_000,
                    )
                    self.acc.note_reconnect(gap_us=gap, resume_seam=True)
                else:
                    self.acc.note_reconnect(gap_us=0, resume_seam=True)

        self.health.record_event_time(c.time_us)
        self.acc.observe(c)

    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        self._started_mono = time.monotonic()
        self._start_run()
        if self.checkpoint_path:
            self.health.load_checkpoint(self.checkpoint_path)

        backoff = RECONNECT_BACKOFF_START_S
        first_connection = True

        try:
            while not self._stop and not self._duration_elapsed():
                url = self._build_url()
                try:
                    async with websockets.connect(
                        url,
                        max_size=10 * 1024 * 1024,
                        ping_interval=30,
                        ping_timeout=10,
                        close_timeout=5,
                    ) as ws:
                        LOG.info("connected %s%s", self.endpoint,
                                 "" if first_connection else " (reconnect)")
                        if not first_connection:
                            self._reconnects += 1
                            self.health.record_reconnect()
                            self._awaiting_first_event_after_resume = (
                                self._resume_from_us is not None
                            )
                        first_connection = False
                        backoff = RECONNECT_BACKOFF_START_S

                        while not self._stop and not self._duration_elapsed():
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                # Silence. Still an observation: let wall clock
                                # close empty windows so a quiet minute inside
                                # a run is recorded, not merely absent.
                                self.acc.tick()
                                self._flush_pending()
                                continue
                            self._handle_raw(raw)
                            self.acc.tick()
                            self._flush_pending()

                except asyncio.CancelledError:
                    raise
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower() or "busy" in str(e).lower():
                        LOG.warning("sqlite busy, retrying: %s", e)
                        await asyncio.sleep(1.0)
                        continue
                    LOG.critical("fatal sqlite error: %s", e)
                    self._stop_reason = "fatal_db_error"
                    raise
                except sqlite3.Error as e:
                    LOG.critical("fatal sqlite error: %s", e)
                    self._stop_reason = "fatal_db_error"
                    raise
                except Exception as e:
                    LOG.warning("connection error (%s: %s)", type(e).__name__, e)

                if self._stop or self._duration_elapsed():
                    break

                # Uncommitted state is disposable: flush what closed, drop the
                # in-flight window, and let the cursor replay reconstruct it.
                self._flush_pending()
                self.acc.discard_open_window()
                delay = min(backoff, RECONNECT_BACKOFF_CAP_S) * (
                    1 + random.random() * 0.2
                )
                LOG.info("reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_CAP_S)

            if self._duration_elapsed() and self._stop_reason == "completed":
                self._stop_reason = "duration_reached"

        finally:
            # A partial final window is committed as partial, never discarded
            # and never rounded up to a full one.
            self.acc.close_for_shutdown()
            try:
                self._flush_pending()
            except Exception:
                LOG.exception("final flush failed; cursor not advanced")
            self._end_run()
            if self.checkpoint_path:
                self.health.write_checkpoint(self.checkpoint_path)
            if self.social_sink is not None:
                try:
                    self.social_sink.close(timeutil.now_us())
                except Exception:
                    LOG.exception("social sink close failed")

    def _duration_elapsed(self) -> bool:
        if not self.duration_s:
            return False
        return (time.monotonic() - self._started_mono) >= self.duration_s
