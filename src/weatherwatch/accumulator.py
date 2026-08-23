"""M3 — in-memory window accumulation and the durable flush.

Single process, single writer, no queue, no writer thread. At M0's measured
~330 eps the per-event cost here is a dict lookup and an integer increment;
the database is touched once per closed window (~once a minute), so the
decoupling machinery driftwatch needed does not apply and is not present.

Two rules govern correctness:

1. **Windows are assigned by stream clock** (`time_us`), never wall clock and
   never `record.createdAt`.
2. **A window closes exactly once**, and its counts, its health row, and the
   resume cursor commit in one transaction.

Windows close either because the stream moved past them (the normal case —
M0 verified `time_us` strictly increasing, so seeing an event in W+1 proves
W is complete) or because wall clock passed the boundary plus a grace period
while the stream was silent. The second path is what makes "observed and
empty" representable at all.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from . import timeutil
from .classify import Classification

LOG = logging.getLogger("weatherwatch.accumulator")

#: How long after a window's end we wait before closing it on wall clock
#: alone. M0 measured end-to-end lag at p50 -0.002s / max 0.12s, so 5s is
#: ~40x headroom. Events arriving after this land in `late_events` and are
#: NOT retroactively added to a committed window.
LATE_EVENT_GRACE_S = 5.0

DEFAULT_BUCKET_WIDTH_S = 60


@dataclass(slots=True)
class ClosedWindow:
    """A window that will never change again. Ready to commit."""

    run_id: str
    bucket_start: int
    bucket_width: int
    counts: dict[str, int]
    events_seen: int
    parse_errors: int
    unclassified: int
    rejected_no_time_us: int
    late_events: int
    reconnects: int
    gap_us: int
    resume_seam: bool
    first_event_us: int | None
    last_event_us: int | None
    observed_duration_us: int
    partial: bool
    lag_ewma_s: float | None = None
    lag_max_s: float | None = None
    coverage_state: str = "unknown"
    gate_reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class _OpenWindow:
    bucket_start: int
    bucket_width: int
    counts: Counter = field(default_factory=Counter)
    events_seen: int = 0
    parse_errors: int = 0
    unclassified: int = 0
    rejected_no_time_us: int = 0
    late_events: int = 0
    reconnects: int = 0
    gap_us: int = 0
    resume_seam: bool = False
    first_event_us: int | None = None
    last_event_us: int | None = None
    #: True when observation began part-way through this window (run start,
    #: or the window that contains a resume seam). Drives coverage maths.
    entered_mid_window: bool = False
    observed_from_us: int | None = None

    @property
    def window_start_us(self) -> int:
        return self.bucket_start * 1_000_000

    @property
    def window_end_us(self) -> int:
        return (self.bucket_start + self.bucket_width) * 1_000_000


#: Metric keys that mean "we saw something we could not interpret". Counted
#: into window_health.unclassified as the schema-drift canary.
_UNCLASSIFIED_METRICS = frozenset({
    "unclassified.kind",
    "untracked.collection",
    "malformed.collection",
    # Compatibility with windows written before the collection states split.
    "unclassified.collection",
    "unclassified.operation",
    "malformed.commit",
})


class Accumulator:
    """Owns the open window and produces ClosedWindow objects.

    Deliberately has no database handle. It is a pure state machine over
    (classification, wall clock) so the commit invariant can be tested
    without I/O, and so there is exactly one place that writes.
    """

    def __init__(
        self,
        run_id: str,
        bucket_width: int = DEFAULT_BUCKET_WIDTH_S,
        grace_s: float = LATE_EVENT_GRACE_S,
        now_us: Callable[[], int] = timeutil.now_us,
    ):
        self.run_id = run_id
        self.bucket_width = bucket_width
        self.grace_us = int(grace_s * 1_000_000)
        self._now_us = now_us

        self._open: _OpenWindow | None = None
        self._closed: list[ClosedWindow] = []
        #: Greatest bucket_start already closed. Guards against a late event
        #: reopening a committed window.
        self._high_water_bucket: int | None = None

        self.first_event_us: int | None = None
        self.last_event_us: int | None = None
        #: Wall clock at the last observed event. Distinguishes "the stream
        #: is quiet" from "we are behind on a busy stream".
        self._last_event_wall_us: int | None = None

        # Pending signals attributed to whichever window is open when the
        # corresponding stream event lands.
        self._pending_reconnects = 0
        self._pending_gap_us = 0
        self._pending_resume_seam = False
        self._pending_mid_window_start = True  # the run's first window
        self._parse_errors_before_first_window = 0
        self._rejected_before_first_window = 0

    # -- signals from the collector ---------------------------------------

    def note_reconnect(self, gap_us: int = 0, resume_seam: bool = False) -> None:
        """Record a reconnect. Attributed to the window that receives the
        next event, or to the currently open window if one exists.

        A reconnect is recorded whether or not it produced a gap. M0's
        survey demonstrated that `time_us` stays strictly monotonic across a
        reconnect that lost ~5,000 events: monotonicity is not evidence of
        completeness, so the seam is recorded structurally rather than
        inferred from the clock.
        """
        if self._open is not None:
            self._open.reconnects += 1
            self._open.gap_us += max(0, gap_us)
            self._open.resume_seam = self._open.resume_seam or resume_seam
        else:
            self._pending_reconnects += 1
            self._pending_gap_us += max(0, gap_us)
            self._pending_resume_seam = self._pending_resume_seam or resume_seam

    def note_parse_error(self) -> None:
        if self._open is not None:
            self._open.parse_errors += 1
        else:
            self._parse_errors_before_first_window += 1

    def note_rejected_no_time_us(self) -> None:
        if self._open is not None:
            self._open.rejected_no_time_us += 1
        else:
            # A frame without stream time cannot choose its own bucket. Carry
            # it into the first observed window, exactly like a parse failure,
            # so cold-start faults do not disappear from accounting.
            self._rejected_before_first_window += 1

    # -- the hot path -------------------------------------------------------

    def observe(self, c: Classification) -> None:
        bucket = timeutil.bucket_start_for(c.time_us, self.bucket_width)

        if self._high_water_bucket is not None and bucket <= self._high_water_bucket:
            # Window already committed. Do not mutate history; record the
            # arrival against the open window so the loss is visible.
            if self._open is not None:
                self._open.late_events += 1
            LOG.warning(
                "late event for closed window %d (high water %d)",
                bucket, self._high_water_bucket,
            )
            return

        if self._open is None:
            self._open_window(bucket)
        elif bucket != self._open.bucket_start:
            # Stream moved on. Everything up to `bucket` is final.
            self._close_open_window(observed_to_us=self._open.window_end_us)
            self._open_window(bucket)

        w = self._open
        assert w is not None
        w.events_seen += 1
        if w.first_event_us is None:
            w.first_event_us = c.time_us
            if w.observed_from_us is None:
                w.observed_from_us = (
                    c.time_us if w.entered_mid_window else w.window_start_us
                )
        w.last_event_us = c.time_us
        for m in c.metrics:
            w.counts[m] += 1
            if m in _UNCLASSIFIED_METRICS:
                w.unclassified += 1

        if self.first_event_us is None:
            self.first_event_us = c.time_us
        self.last_event_us = c.time_us
        self._last_event_wall_us = self._now_us()

    # -- window lifecycle ---------------------------------------------------

    def _open_window(self, bucket_start: int) -> None:
        w = _OpenWindow(bucket_start=bucket_start, bucket_width=self.bucket_width)
        w.reconnects = self._pending_reconnects
        w.gap_us = self._pending_gap_us
        w.resume_seam = self._pending_resume_seam
        w.entered_mid_window = self._pending_mid_window_start
        if not w.entered_mid_window:
            w.observed_from_us = w.window_start_us
        w.parse_errors = self._parse_errors_before_first_window
        w.rejected_no_time_us = self._rejected_before_first_window
        self._parse_errors_before_first_window = 0
        self._rejected_before_first_window = 0
        self._pending_reconnects = 0
        self._pending_gap_us = 0
        self._pending_resume_seam = False
        self._pending_mid_window_start = False
        self._open = w

    def _close_open_window(self, observed_to_us: int) -> None:
        w = self._open
        if w is None:
            return
        self._open = None
        self._high_water_bucket = max(
            w.bucket_start, self._high_water_bucket or w.bucket_start
        )

        observed_from = w.observed_from_us
        if observed_from is None:
            observed_from = w.window_start_us
        observed_to = min(observed_to_us, w.window_end_us)
        span = max(0, observed_to - observed_from)
        observed_duration = max(0, span - w.gap_us)
        full = self.bucket_width * 1_000_000

        self._closed.append(ClosedWindow(
            run_id=self.run_id,
            bucket_start=w.bucket_start,
            bucket_width=w.bucket_width,
            counts=dict(w.counts),
            events_seen=w.events_seen,
            parse_errors=w.parse_errors,
            unclassified=w.unclassified,
            rejected_no_time_us=w.rejected_no_time_us,
            late_events=w.late_events,
            reconnects=w.reconnects,
            gap_us=w.gap_us,
            resume_seam=w.resume_seam,
            first_event_us=w.first_event_us,
            last_event_us=w.last_event_us,
            observed_duration_us=observed_duration,
            partial=observed_duration < full,
        ))

    def tick(self) -> None:
        """Close windows that wall clock has moved past, but only while the
        stream is idle.

        With events flowing, windows close on stream progression, which is
        exact. Wall clock is a fallback for silence, and it must not be used
        while events are still arriving: a lagging consumer has a wall clock
        far ahead of its stream clock — M0's slow-consumer probe measured a
        75 s lag with the connection healthy and no events lost — and closing
        on wall clock there would shut windows that still had events coming
        and misfile them as `late_events`. Lag is not silence.
        """
        now = self._now_us()
        if self._last_event_wall_us is not None:
            if now - self._last_event_wall_us < self.grace_us:
                return  # events still arriving; let the stream close windows
        while self._open is not None and now >= self._open.window_end_us + self.grace_us:
            end = self._open.window_end_us
            self._close_open_window(observed_to_us=end)
            # Keep marching so silence produces a contiguous run of empty
            # observed windows rather than one gap.
            next_start = end // 1_000_000
            if now >= (next_start + self.bucket_width) * 1_000_000 + self.grace_us:
                self._open_window(next_start)
                self._open.observed_from_us = self._open.window_start_us
            else:
                break

    def discard_open_window(self) -> None:
        """Drop the in-flight window's counts before a reconnect replay.

        The window was never committed, so nothing durable is lost. Its
        interval is reconstructed from `persisted_cursor + 1`, which M0
        showed resumes exactly. The *observational* facts — that a reconnect
        happened, how big the gap was, that a seam exists — are promoted to
        pending so they land on the reconstructed window. Losing those would
        make the replay look like uninterrupted observation, which is the
        exact dishonesty this design exists to prevent.
        """
        w = self._open
        if w is None:
            return
        self._pending_reconnects += w.reconnects
        self._pending_gap_us += w.gap_us
        self._pending_resume_seam = True
        # The reconstructed window is re-entered part-way through only if
        # this one was; a full replay restores the whole window.
        self._pending_mid_window_start = w.entered_mid_window
        self._open = None

    def close_for_shutdown(self) -> None:
        """Close the in-flight window at the last event we actually saw.

        The window is committed as `partial` rather than discarded: the counts
        are real observations, and the resume cursor will point at their last
        event, so the next run continues from exactly there. A later run
        recounts the remainder of the same wall-clock window under its own
        run_id — non-overlapping, same endpoint, so the two rows sum correctly.
        """
        if self._open is None:
            return
        w = self._open
        observed_to = w.last_event_us if w.last_event_us else w.window_start_us
        self._close_open_window(observed_to_us=observed_to)

    # -- handoff to the writer ---------------------------------------------

    def take_closed(self) -> list[ClosedWindow]:
        out = self._closed
        self._closed = []
        return out

    @staticmethod
    def commit_cursor_for(windows: list[ClosedWindow]) -> int | None:
        """The greatest `time_us` durably represented by these windows.

        This is the entire cursor invariant in one function. Windows with no
        events contribute nothing to it — an empty window proves we were
        watching, not that the stream advanced, so it must not push the
        cursor past events we never saw.
        """
        seen = [w.last_event_us for w in windows if w.last_event_us is not None]
        return max(seen) if seen else None

    @property
    def open_window_start(self) -> int | None:
        return self._open.bucket_start if self._open else None
