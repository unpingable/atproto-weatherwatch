"""M5 — read side."""

from __future__ import annotations

import pytest

from weatherwatch import query, read
from tests.conftest import SYNTH_BASE, SYNTH_WIDTH, build_run

EP_A = "wss://relay-a.invalid/subscribe"
EP_B = "wss://relay-b.invalid/subscribe"


def w(count=100, **kw):
    kw.setdefault("metrics", {"post.create": count})
    return kw


# --- one run ---------------------------------------------------------------

def test_single_run_series_counts_and_rates(conn):
    build_run(conn, "r1", [w(100), w(200), w(300)])
    s = query.series(conn, ["r1"], "post.create")
    assert [p.count for p in s.points] == [100, 200, 300]
    assert [p.rate for p in s.points] == pytest.approx([100 / 60, 200 / 60, 300 / 60])
    assert s.total == 600
    assert s.observed_seconds == 180.0
    assert round(s.mean_rate, 4) == round(600 / 180, 4)


def test_endpoint_survives_into_query_output(conn):
    build_run(conn, "r1", [w(10)], endpoint=EP_A)
    s = query.series(conn, ["r1"], "post.create")
    assert s.endpoint == EP_A
    assert query.run_summary(conn, "r1").endpoint == EP_A


def test_run_summary_fields(conn):
    build_run(conn, "r1", [w(10), w(20, gap_us=5_000_000)])
    r = query.run_summary(conn, "r1")
    assert r.windows == 2
    assert r.gap_windows == 1
    assert r.gap_us == 5_000_000
    assert r.events == 30
    assert r.status == query.RUN_PARTIAL  # a gap inside the run


def test_run_status_vocabulary(conn):
    build_run(conn, "clean1", [w(10)])
    assert query.run_summary(conn, "clean1").status == query.RUN_COMPLETE

    build_run(conn, "open1", [w(10)], ended_at=None)
    assert query.run_summary(conn, "open1").status == query.RUN_OPEN

    build_run(conn, "crash1", [w(10)], stop_reason="fatal_db_error")
    assert query.run_summary(conn, "crash1").status == query.RUN_INTERRUPTED

    build_run(conn, "gap1", [w(10, gap_us=1_000_000)])
    assert query.run_summary(conn, "gap1").status == query.RUN_PARTIAL


# --- combination rules -----------------------------------------------------

def test_sequential_same_endpoint_runs_combine(conn):
    build_run(conn, "r1", [w(10), w(20)], start_index=0,
              started_at="2026-01-01T00:00:00+00:00",
              ended_at="2026-01-01T00:02:00+00:00")
    build_run(conn, "r2", [w(30), w(40)], start_index=5,
              started_at="2026-01-01T00:05:00+00:00",
              ended_at="2026-01-01T00:07:00+00:00")
    read.assert_summable(conn, ["r1", "r2"])
    s = query.series(conn, ["r1", "r2"], "post.create")
    assert s.total == 100
    observed = [p for p in s.points if p.observed]
    assert len(observed) == 4


def test_different_endpoints_refuse(conn):
    build_run(conn, "r1", [w(10)], endpoint=EP_A)
    build_run(conn, "r2", [w(10)], endpoint=EP_B, start_index=10,
              started_at="2026-01-01T02:00:00+00:00",
              ended_at="2026-01-01T02:01:00+00:00")
    with pytest.raises(read.NotSummable, match="different endpoints"):
        query.series(conn, ["r1", "r2"], "post.create")


def test_overlapping_runs_refuse(conn):
    build_run(conn, "r1", [w(10), w(10), w(10)], start_index=0)
    build_run(conn, "r2", [w(10), w(10), w(10)], start_index=1)
    with pytest.raises(read.NotSummable, match="overlapping"):
        query.series(conn, ["r1", "r2"], "post.create")


def test_compatible_runs_stops_at_first_conflict(conn):
    build_run(conn, "old", [w(10)], start_index=0,
              started_at="2026-01-01T00:00:00+00:00",
              ended_at="2026-01-01T00:01:00+00:00")
    build_run(conn, "mid", [w(10)], start_index=5,
              started_at="2026-01-01T00:05:00+00:00",
              ended_at="2026-01-01T00:06:00+00:00")
    build_run(conn, "overlap", [w(10)], start_index=5,
              started_at="2026-01-01T00:05:30+00:00",
              ended_at="2026-01-01T00:06:30+00:00")
    chosen = query.compatible_runs(conn, EP_A)
    read.assert_summable(conn, chosen)  # whatever it picked must be summable
    assert "overlap" in chosen or "mid" in chosen


# --- unobserved vs zero ----------------------------------------------------

def test_unobserved_is_none_not_zero(conn):
    # window 1 is observed with zero posts; window 2 was never observed
    build_run(conn, "r1", [
        w(10),
        {"metrics": {"like.create": 5}},   # observed, no post.create at all
        None,                               # UNOBSERVED
        w(30),
    ])
    s = query.series(conn, ["r1"], "post.create")
    counts = [p.count for p in s.points]
    assert counts == [10, 0, None, 30], counts

    observed_zero = s.points[1]
    unobserved = s.points[2]
    assert observed_zero.observed is True
    assert observed_zero.count == 0
    assert observed_zero.rate == 0.0
    assert unobserved.observed is False
    assert unobserved.count is None
    assert unobserved.rate is None
    assert unobserved.quality == "unobserved"


def test_totals_ignore_unobserved_windows(conn):
    build_run(conn, "r1", [w(60), None, w(60)])
    s = query.series(conn, ["r1"], "post.create")
    assert s.total == 120
    assert s.observed_seconds == 120.0  # NOT 180
    assert s.mean_rate == 1.0


def test_densify_can_be_disabled(conn):
    build_run(conn, "r1", [w(10), None, w(10)])
    dense = query.series(conn, ["r1"], "post.create")
    sparse = query.series(conn, ["r1"], "post.create", densify=False)
    assert len(dense.points) == 3
    assert len(sparse.points) == 2
    assert all(p.observed for p in sparse.points)


def test_absurd_range_refuses_rather_than_truncating(conn):
    build_run(conn, "r1", [w(10)])
    with pytest.raises(query.QueryTooLarge):
        query.series(conn, ["r1"], "post.create",
                     since=SYNTH_BASE, until=SYNTH_BASE + 60 * 999_999,
                     max_points=100)


# --- partial windows -------------------------------------------------------

def test_partial_window_rate_uses_observed_duration(conn):
    """A 15-second window with 50 events is 3.33/s, not 0.83/s."""
    build_run(conn, "r1", [w(50, observed_us=15_000_000)])
    p = query.series(conn, ["r1"], "post.create").points[0]
    assert p.quality == "partial"
    assert p.observed_seconds == 15.0
    assert round(p.rate, 4) == round(50 / 15, 4)
    assert p.baseline_eligible is False


def test_partial_window_does_not_inflate_observed_seconds(conn):
    build_run(conn, "r1", [w(60), w(30, observed_us=30_000_000)])
    s = query.series(conn, ["r1"], "post.create")
    assert s.observed_seconds == 90.0
    assert s.mean_rate == 1.0


# --- flags and conditioning ------------------------------------------------

def test_gap_and_degraded_windows_stay_marked(conn):
    build_run(conn, "r1", [
        w(10, gap_us=10_000_000),
        w(10, coverage_state="degraded", gate_reasons="low_eps"),
        w(10, parse_errors=3),
        w(10, resume_seam=1),
    ])
    s = query.series(conn, ["r1"], "post.create")
    q = [p.quality for p in s.points]
    assert q == ["gap", "degraded", "loss", "seam"]
    assert [p.baseline_eligible for p in s.points] == [False, False, False, True]


def test_lag_only_degradation_is_not_a_coverage_defect(conn):
    """Resuming from a cursor replays backlog: the collector is far behind
    real time while missing nothing. Such a window's counts are exact."""
    build_run(conn, "r1", [
        w(10, coverage_state="degraded", gate_reasons="lag_high",
          lag_ewma_s=600.0),
    ])
    p = query.series(conn, ["r1"], "post.create").points[0]
    assert p.quality == "lagged"
    assert p.baseline_eligible is True
    assert p.coverage_state == "degraded"  # raw value preserved


def test_recovery_hysteresis_is_not_a_coverage_defect(conn):
    """DEGRADED with no active gate reason is the recovery streak, not loss."""
    build_run(conn, "r1", [w(10, coverage_state="degraded", gate_reasons=None)])
    p = query.series(conn, ["r1"], "post.create").points[0]
    assert p.quality == "recovering"
    assert p.baseline_eligible is True


def test_lag_plus_real_defect_stays_degraded(conn):
    build_run(conn, "r1", [
        w(10, coverage_state="degraded", gate_reasons="lag_high",
          gap_us=5_000_000),
    ])
    p = query.series(conn, ["r1"], "post.create").points[0]
    assert query.FLAG_DEGRADED in p.flags
    assert p.baseline_eligible is False


def test_warming_up_is_not_baseline_eligible(conn):
    build_run(conn, "r1", [w(10, coverage_state="warming_up")])
    p = query.series(conn, ["r1"], "post.create").points[0]
    assert p.quality == "warming_up"
    assert p.baseline_eligible is False


# --- totals ----------------------------------------------------------------

def test_total_events_series_uses_events_seen(conn):
    build_run(conn, "r1", [
        {"metrics": {"post.create": 5, "post.create.reply": 3},
         "events_seen": 5},   # a reply emits two metrics but is one event
    ])
    s = query.total_events_series(conn, ["r1"])
    assert s.points[0].count == 5


def test_metric_totals_and_available_metrics(conn):
    build_run(conn, "r1", [{"metrics": {"post.create": 4, "like.create": 9}}])
    assert query.metric_totals(conn, ["r1"]) == {"like.create": 9, "post.create": 4}
    assert query.available_metrics(conn, ["r1"]) == ["like.create", "post.create"]


def test_densify_does_not_invent_unobserved_time_outside_the_span(conn):
    """Holes BETWEEN observations are knowable and are filled as unobserved.
    Time before the first or after the last observed window is not: we have no
    basis for claiming observation "should" have continued, so the series
    simply ends. Inventing trailing unobserved windows would imply an
    intended coverage that was never declared.
    """
    build_run(conn, "r1", [w(10), None, w(10)])
    s = query.series(conn, ["r1"], "post.create")
    assert len(s.points) == 3
    assert [p.observed for p in s.points] == [True, False, True]
    assert s.points[0].bucket_start == SYNTH_BASE
    assert s.points[-1].bucket_start == SYNTH_BASE + 2 * SYNTH_WIDTH
