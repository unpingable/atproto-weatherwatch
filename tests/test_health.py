"""Observer honesty (M4)."""

from __future__ import annotations

import time

import pytest

from weatherwatch import health
from weatherwatch.health import DEGRADED, OK, WARMING_UP, ObservationHealth

CLEAN = {
    "parse_errors": 0, "rejected_no_time_us": 0, "late_events": 0,
    "gap_us": 0, "unclassified": 0,
}


def losses(**kw):
    return {**CLEAN, **kw}


def warm(h, eps=330.0, windows=health.WARMUP_WINDOWS):
    for _ in range(windows):
        h.record_window(int(eps * 60), 60.0, losses())
    return h


# --- the headline rule -----------------------------------------------------

def test_monotonic_cursor_never_implies_completeness():
    """M0: a live reconnect dropped ~5,000 events while time_us stayed
    strictly increasing across the seam. Health must not derive coverage
    from ordering, and must say so out loud."""
    h = warm(ObservationHealth())
    snap = h.snapshot()
    assert snap["cursor_monotonicity_implies_completeness"] is False


def test_gap_degrades_immediately_despite_perfect_ordering():
    h = warm(ObservationHealth())
    assert h.snapshot()["coverage_state"] == OK
    snap = h.record_window(330 * 60, 60.0, losses(gap_us=30_000_000))
    assert "gap_observed" in snap["gate_reasons"]
    assert snap["coverage_state"] == DEGRADED


def test_uninstrumented_loss_path_refuses_to_report_health():
    """Driftwatch 2026-04-30: a green signal is inadmissible when a known
    shedding path has no bucket. Make that mechanical."""
    h = ObservationHealth()
    incomplete = dict(CLEAN)
    del incomplete["late_events"]
    with pytest.raises(RuntimeError, match="uninstrumented loss path"):
        h.record_window(100, 60.0, incomplete)


def test_known_loss_paths_are_all_present_in_a_real_window():
    from weatherwatch.accumulator import Accumulator
    from weatherwatch.classify import Classification
    acc = Accumulator("r", bucket_width=60)
    acc.observe(Classification(1_700_000_000_000_000, ("post.create",)))
    acc.observe(Classification(1_700_000_060_000_000, ("post.create",)))
    w = acc.take_closed()[0]
    sample = {
        "parse_errors": w.parse_errors,
        "rejected_no_time_us": w.rejected_no_time_us,
        "late_events": w.late_events,
        "gap_us": w.gap_us,
        "unclassified": w.unclassified,
    }
    health.assert_loss_buckets_instrumented(sample)  # must not raise
    assert set(sample) == set(health.KNOWN_LOSS_PATHS)


# --- state machine ---------------------------------------------------------

def test_starts_warming_up():
    assert ObservationHealth().snapshot()["coverage_state"] == WARMING_UP


def test_warmup_to_ok():
    assert warm(ObservationHealth()).snapshot()["coverage_state"] == OK


def test_sustained_low_eps_degrades_after_streak():
    h = warm(ObservationHealth(), eps=330.0)
    for i in range(health.CONSECUTIVE_BAD_WINDOWS):
        snap = h.record_window(int(10 * 60), 60.0, losses())
        if i < health.CONSECUTIVE_BAD_WINDOWS - 1:
            assert "low_eps" not in snap["gate_reasons"]
    assert "low_eps" in snap["gate_reasons"]
    assert snap["coverage_state"] == DEGRADED


def test_loss_fraction_degrades():
    h = warm(ObservationHealth())
    for _ in range(health.CONSECUTIVE_BAD_WINDOWS):
        snap = h.record_window(100, 60.0, losses(parse_errors=50))
    assert "loss_observed" in snap["gate_reasons"]


def test_baseline_frozen_once_degraded():
    """The baseline keeps learning while healthy, so it does drift during the
    first few bad windows before the streak trips. Once DEGRADED, it must
    stop: otherwise a degraded stream teaches the collector that degraded is
    normal, and coverage silently recovers to 100% of nothing."""
    h = warm(ObservationHealth(), eps=330.0)
    for _ in range(health.CONSECUTIVE_BAD_WINDOWS):
        h.record_window(10, 60.0, losses())
    assert h.snapshot()["coverage_state"] == DEGRADED
    frozen = h.snapshot()["baseline_eps"]
    for _ in range(10):
        h.record_window(10, 60.0, losses())
    assert h.snapshot()["baseline_eps"] == frozen


# --- lag -------------------------------------------------------------------

def test_lag_rise_is_visible_without_any_disconnect():
    """M0's slow-consumer probe: a 75s stall produced a 75.09s lag with the
    connection still open and zero events lost. Lag is the loss signal for
    that failure mode, so it must move without a reconnect."""
    h = warm(ObservationHealth())
    assert h.snapshot()["stream_lag_s"] < 1.0
    stale = int((time.time() - 75) * 1_000_000)
    for _ in range(40):
        h.record_event_time(stale)
    snap = h.snapshot()
    assert snap["stream_lag_s"] > 60, snap
    assert snap["reconnects"] == 0, "no disconnect occurred"


def test_negative_lag_is_clamped():
    """M0 measured p50 lag of -0.002s: this host's clock runs ahead of the
    relay. Un-clamped, the EWMA drifts negative and hides a real stall."""
    h = ObservationHealth()
    future = int((time.time() + 30) * 1_000_000)
    for _ in range(20):
        h.record_event_time(future)
    assert h.snapshot()["stream_lag_s"] >= 0.0


def test_sustained_lag_degrades():
    h = warm(ObservationHealth())
    stale = int((time.time() - 300) * 1_000_000)
    for _ in range(60):
        h.record_event_time(stale)
    for _ in range(health.CONSECUTIVE_BAD_WINDOWS):
        snap = h.record_window(330 * 60, 60.0, losses())
    assert "lag_high" in snap["gate_reasons"]


# --- checkpoint ------------------------------------------------------------

def test_checkpoint_roundtrip(tmp_path):
    h = warm(ObservationHealth(), eps=330.0)
    path = tmp_path / "baseline.json"
    assert h.write_checkpoint(path)
    h2 = ObservationHealth()
    assert h2.load_checkpoint(path)
    assert h2.snapshot()["baseline_eps"] == pytest.approx(
        h.snapshot()["baseline_eps"], rel=0.01)
    assert h2.snapshot()["baseline_restored"] is True


def test_stale_checkpoint_is_rejected(tmp_path):
    h = warm(ObservationHealth())
    data = h.checkpoint()
    data["checkpoint_at"] = time.time() - 99999
    path = tmp_path / "old.json"
    path.write_text(__import__("json").dumps(data))
    assert ObservationHealth().load_checkpoint(path) is False


def test_restore_does_not_resurrect_a_stale_degraded_state(tmp_path):
    h = warm(ObservationHealth())
    for _ in range(health.CONSECUTIVE_BAD_WINDOWS + 1):
        h.record_window(1, 60.0, losses())
    assert h.snapshot()["coverage_state"] == DEGRADED
    path = tmp_path / "cp.json"
    h.write_checkpoint(path)
    h2 = ObservationHealth()
    h2.load_checkpoint(path)
    assert h2.snapshot()["coverage_state"] == OK, (
        "state is re-derived from the live stream, not inherited"
    )


def test_missing_checkpoint_is_a_cold_start_not_an_error(tmp_path):
    assert ObservationHealth().load_checkpoint(tmp_path / "nope.json") is False
