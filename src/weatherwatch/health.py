"""M4 — observer honesty.

Adapted from `driftwatch/src/labeler/platform_health.py`. Kept: the EWMA
throughput baseline, the coverage ratio, the lag EWMA with negative clamping,
the three-state machine with hysteresis and recalibration, and the
checkpoint/restore. Dropped: `get_detection()` and everything it dragged in
(SubjectRef, DetectionEnvelope, receipt hashing) — this project has no
subjects to make claims about.

Thresholds are driftwatch's conservative defaults, unchanged. M0 measured
mean 330 eps / p95 392 / max 488 and end-to-end lag under 0.12 s, which the
defaults accommodate comfortably; retuning without a failure to point at
would be guessing. TODOs mark the two that most likely want revisiting.

    THE POINT OF THIS MODULE

    A monotonic cursor is not evidence of completeness. M0 watched a live
    reconnect drop roughly 5,000 events while `time_us` stayed strictly
    increasing across the seam — 198,249 of 198,249 transitions forward, zero
    backward, through a hole. Nothing derived from clock ordering can detect
    that. Coverage is asserted only from instrumented loss buckets, and
    `assert_loss_buckets_instrumented()` below exists so a green state can
    never be reported by a path that simply has no counter attached.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

LOG = logging.getLogger("weatherwatch.health")

WARMUP_WINDOWS = int(os.getenv("WW_HEALTH_WARMUP_WINDOWS", "5"))
EWMA_SPAN = int(os.getenv("WW_HEALTH_EWMA_SPAN", "30"))
EWMA_ALPHA = 2.0 / (EWMA_SPAN + 1)

COVERAGE_LOW_THRESHOLD = float(os.getenv("WW_HEALTH_COVERAGE_LOW", "0.6"))
# TODO(threshold): driftwatch used 120s against a subject-recheck gate. M0
# measured p95 lag at 0.008s, so this is ~15,000x headroom and will only fire
# on a genuine stall. Tighten once a real degraded run has been observed.
LAG_HIGH_THRESHOLD_S = float(os.getenv("WW_HEALTH_LAG_HIGH_S", "120"))
LOSS_FRAC_HIGH_THRESHOLD = float(os.getenv("WW_HEALTH_LOSS_FRAC_HIGH", "0.02"))
CONSECUTIVE_BAD_WINDOWS = int(os.getenv("WW_HEALTH_BAD_WINDOWS", "3"))

COVERAGE_RECOVER_THRESHOLD = float(os.getenv("WW_HEALTH_COVERAGE_RECOVER", "0.8"))
LAG_RECOVER_THRESHOLD_S = float(os.getenv("WW_HEALTH_LAG_RECOVER_S", "30"))
CONSECUTIVE_GOOD_WINDOWS = int(os.getenv("WW_HEALTH_GOOD_WINDOWS", "5"))
RECALIBRATION_WINDOWS = int(os.getenv("WW_HEALTH_RECALIBRATION_WINDOWS", "3"))

LAG_EWMA_ALPHA = float(os.getenv("WW_HEALTH_LAG_EWMA_ALPHA", "0.2"))
LAG_CLAMP_MAX_S = 600.0

_EPS = 1e-6

WARMING_UP = "warming_up"
OK = "ok"
DEGRADED = "degraded"

#: Every way this collector can fail to observe something inside a run. Each
#: MUST have a counter feeding `record_window`. Driftwatch's 2026-04-30 scar:
#: "a green recovery signal is structurally inadmissible if any known shedding
#: path has no instrumented loss bucket." The queue-overflow and
#: writer-rollback paths that dominated driftwatch do not exist here — there
#: is no queue and no writer thread — so this list is short by construction,
#: not by omission.
KNOWN_LOSS_PATHS = frozenset({
    "parse_errors",          # frame arrived, JSON did not
    "rejected_no_time_us",   # unassignable to any window
    "late_events",           # arrived after its window committed
    "gap_us",                # stream discontinuity across a reconnect
    "unclassified",          # counted, but its meaning was not recovered
})


def assert_loss_buckets_instrumented(sample: dict) -> None:
    """Refuse to report health from a window missing a loss bucket.

    Called on every window. Cheap, and it makes the parole rule mechanical
    rather than aspirational.
    """
    missing = KNOWN_LOSS_PATHS - set(sample)
    if missing:
        raise RuntimeError(
            "health refused: uninstrumented loss path(s) "
            f"{sorted(missing)} — a green state from this window would be a "
            "claim about buckets that do not exist"
        )


class ObservationHealth:
    """Thread-safe, though this collector is single-threaded by design."""

    CHECKPOINT_VERSION = 1
    MAX_CHECKPOINT_AGE_S = 3600

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._state = WARMING_UP
        self._windows_seen = 0
        self._baseline_eps = 0.0
        self._current_eps = 0.0
        self._stream_lag_s = 0.0
        self._lag_max_s = 0.0
        self._loss_frac = 0.0
        self._reconnects = 0
        self._low_coverage_streak = 0
        self._high_lag_streak = 0
        self._high_loss_streak = 0
        self._gap_streak = 0
        self._recovery_streak = 0
        self._recalibration_remaining = 0
        self._gate_reasons: list[str] = []
        self._baseline_restored = False

    # -- per-event ---------------------------------------------------------

    def record_event_time(self, time_us: int) -> None:
        """Update the lag EWMA from one event's stream timestamp.

        Negative lag is clamped to zero: M0 measured a p50 of -0.002 s on this
        host because the local clock runs ~2 ms ahead of the relay. Without
        the clamp the EWMA drifts negative and a real stall takes longer to
        register.
        """
        if not time_us:
            return
        raw = (time.time() * 1_000_000 - time_us) / 1_000_000
        raw = min(max(0.0, raw), LAG_CLAMP_MAX_S)
        with self._lock:
            if self._stream_lag_s == 0.0:
                self._stream_lag_s = raw
            else:
                self._stream_lag_s = (
                    LAG_EWMA_ALPHA * raw + (1 - LAG_EWMA_ALPHA) * self._stream_lag_s
                )
            self._lag_max_s = max(self._lag_max_s, raw)

    def record_reconnect(self) -> None:
        with self._lock:
            self._reconnects += 1

    # -- per-window --------------------------------------------------------

    def record_window(
        self,
        events_in: int,
        window_secs: float,
        losses: dict,
    ) -> dict:
        """Fold one closed window into the health state.

        `losses` must carry every key in KNOWN_LOSS_PATHS.
        """
        assert_loss_buckets_instrumented(losses)
        with self._lock:
            return self._record_window_locked(events_in, window_secs, losses)

    def _record_window_locked(self, events_in, window_secs, losses) -> dict:
        self._windows_seen += 1
        window_secs = max(float(window_secs), 1.0)
        self._current_eps = events_in / window_secs

        if self._state != DEGRADED:
            if self._baseline_eps == 0.0:
                self._baseline_eps = self._current_eps
            else:
                self._baseline_eps = (
                    EWMA_ALPHA * self._current_eps
                    + (1 - EWMA_ALPHA) * self._baseline_eps
                )

        lost = (
            int(losses["parse_errors"])
            + int(losses["rejected_no_time_us"])
            + int(losses["late_events"])
        )
        self._loss_frac = lost / max(events_in + lost, 1)
        gap_us = int(losses["gap_us"])

        coverage = min(self._current_eps / max(self._baseline_eps, _EPS), 1.0)

        reasons: list[str] = []

        self._low_coverage_streak = (
            self._low_coverage_streak + 1 if coverage < COVERAGE_LOW_THRESHOLD else 0
        )
        if self._low_coverage_streak >= CONSECUTIVE_BAD_WINDOWS:
            reasons.append("low_eps")

        self._high_lag_streak = (
            self._high_lag_streak + 1 if self._stream_lag_s > LAG_HIGH_THRESHOLD_S else 0
        )
        if self._high_lag_streak >= CONSECUTIVE_BAD_WINDOWS:
            reasons.append("lag_high")

        self._high_loss_streak = (
            self._high_loss_streak + 1 if self._loss_frac > LOSS_FRAC_HIGH_THRESHOLD else 0
        )
        if self._high_loss_streak >= CONSECUTIVE_BAD_WINDOWS:
            reasons.append("loss_observed")

        # A gap degrades the very window it lands in. It is a discontinuity
        # in observation, not a trend, so it needs no streak.
        if gap_us > 0:
            reasons.append("gap_observed")

        self._gate_reasons = reasons

        if self._state == WARMING_UP:
            if self._windows_seen >= WARMUP_WINDOWS:
                self._state = DEGRADED if reasons else OK
                LOG.info("health: WARMING_UP -> %s %s", self._state, reasons or "")
        elif self._state == OK:
            if reasons:
                self._state = DEGRADED
                self._recovery_streak = 0
                LOG.warning(
                    "health: OK -> DEGRADED reasons=%s coverage=%.1f%% lag=%.2fs",
                    reasons, coverage * 100, self._stream_lag_s,
                )
        elif self._state == DEGRADED:
            recovered = (
                coverage > COVERAGE_RECOVER_THRESHOLD
                and self._stream_lag_s < LAG_RECOVER_THRESHOLD_S
                and not reasons
            )
            self._recovery_streak = self._recovery_streak + 1 if recovered else 0
            if self._recovery_streak >= CONSECUTIVE_GOOD_WINDOWS:
                self._state = OK
                self._recalibration_remaining = RECALIBRATION_WINDOWS
                self._recovery_streak = 0
                LOG.info("health: DEGRADED -> OK (recalibrating %d windows)",
                         RECALIBRATION_WINDOWS)

        if self._recalibration_remaining > 0:
            self._recalibration_remaining -= 1

        snap = self._snapshot_locked(coverage)
        self._lag_max_s = 0.0
        self._reconnects = 0
        return snap

    # -- reporting ---------------------------------------------------------

    def is_degraded(self) -> bool:
        with self._lock:
            return self._state == DEGRADED or self._recalibration_remaining > 0

    def snapshot(self) -> dict:
        with self._lock:
            coverage = min(self._current_eps / max(self._baseline_eps, _EPS), 1.0)
            return self._snapshot_locked(coverage)

    def _snapshot_locked(self, coverage: float) -> dict:
        return {
            "coverage_state": self._state,
            "coverage_pct": round(coverage, 4),
            "current_eps": round(self._current_eps, 1),
            "baseline_eps": round(self._baseline_eps, 1),
            "stream_lag_s": round(self._stream_lag_s, 3),
            "lag_max_s": round(self._lag_max_s, 3),
            "loss_frac": round(self._loss_frac, 4),
            "reconnects": self._reconnects,
            "gate_reasons": list(self._gate_reasons),
            "windows_seen": self._windows_seen,
            "baseline_restored": self._baseline_restored,
            # Present so nobody has to remember. Ordering says nothing about
            # completeness; see the module docstring.
            "cursor_monotonicity_implies_completeness": False,
        }

    # -- checkpoint --------------------------------------------------------

    def checkpoint(self) -> dict:
        with self._lock:
            return {
                "version": self.CHECKPOINT_VERSION,
                "baseline_eps": self._baseline_eps,
                "current_eps": self._current_eps,
                "stream_lag_s": self._stream_lag_s,
                "windows_seen": self._windows_seen,
                "checkpoint_at": time.time(),
            }

    def restore(self, data: dict) -> bool:
        """Restore only the EWMA baseline, and only if fresh.

        Streaks, gate reasons and state are deliberately not restored: they
        describe a stream we are no longer attached to. A restart re-derives
        them. Restoring a stale DEGRADED would be a claim about the present
        based on the past.
        """
        if not data or data.get("version") != self.CHECKPOINT_VERSION:
            return False
        if time.time() - data.get("checkpoint_at", 0) > self.MAX_CHECKPOINT_AGE_S:
            LOG.info("baseline checkpoint stale, cold start")
            return False
        baseline = data.get("baseline_eps", 0)
        if baseline <= 0:
            return False
        with self._lock:
            self._baseline_eps = baseline
            self._current_eps = data.get("current_eps", baseline)
            self._stream_lag_s = data.get("stream_lag_s", 0.0)
            self._windows_seen = data.get("windows_seen", WARMUP_WINDOWS)
            if self._windows_seen >= WARMUP_WINDOWS:
                self._state = OK
            self._baseline_restored = True
            LOG.info("baseline restored: %.1f eps", baseline)
            return True

    def write_checkpoint(self, path: Path) -> bool:
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.checkpoint()))
            tmp.replace(path)
            return True
        except OSError as e:
            LOG.warning("checkpoint write failed: %s", e)
            return False

    def load_checkpoint(self, path: Path) -> bool:
        try:
            return self.restore(json.loads(path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
