"""M6 — derived weather. All assertions are against deterministic inputs."""

from __future__ import annotations

import pytest

from weatherwatch import derive, query
from tests.conftest import build_run

EP_A = "wss://relay-a.invalid/subscribe"


def w(count=60, metric="post.create", **kw):
    kw.setdefault("metrics", {metric: count})
    return kw


# --- primitives ------------------------------------------------------------

def test_ratio_denominator_zero_is_none_not_infinity():
    assert derive.ratio(5, 0) is None
    assert derive.ratio(0, 0) is None
    assert derive.ratio(None, 5) is None
    assert derive.ratio(5, None) is None
    assert derive.ratio(1, 4) == 0.25


def test_ratio_zero_numerator_is_a_real_zero():
    assert derive.ratio(0, 10) == 0.0


def test_mean_and_stddev_are_exact():
    assert derive.mean([1, 2, 3, 4]) == 2.5
    assert derive.mean([]) is None
    # sample stddev of 2,4,4,4,5,5,7,9 is 2.13809...
    assert derive.stddev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, rel=1e-4)
    assert derive.stddev([5]) is None
    assert derive.stddev([3, 3, 3]) == 0.0


def test_zscore_and_pct_change_guard_zero_denominators():
    assert derive.zscore(10, 5, 2.5) == 2.0
    assert derive.zscore(10, 5, 0) is None
    assert derive.zscore(10, None, 2) is None
    assert derive.pct_change(15, 10) == 0.5
    assert derive.pct_change(15, 0) is None


@pytest.mark.parametrize("z,expected", [
    (5.0, "surging"), (3.0, "surging"), (2.0, "elevated"), (1.5, "elevated"),
    (0.0, "normal"), (-1.0, "normal"), (-1.5, "quiet"), (-2.9, "quiet"),
    (-3.0, "degrading"), (-9.0, "degrading"), (None, "unknown"),
])
def test_condition_thresholds_are_exact(z, expected):
    assert derive.condition(z) == expected


# --- rolling baselines -----------------------------------------------------

def test_rolling_baseline_is_deterministic_and_excludes_current_window(conn):
    """Baseline for window k is the eligible windows strictly before k."""
    build_run(conn, "r1", [w(60) for _ in range(10)] + [w(120)])
    s = query.series(conn, ["r1"], "post.create")
    deps = derive.rolling_departures(s, n=10, min_samples=5)

    last = deps[-1]
    assert last.value == pytest.approx(2.0)       # 120 / 60s
    assert last.baseline_mean == pytest.approx(1.0)  # all prior windows 60/60s
    assert last.baseline_std == 0.0
    assert last.z is None, "zero variance must not yield an infinite z"
    assert last.condition == "unknown"

    # Determinism: same input, same output.
    assert derive.rolling_departures(s, n=10, min_samples=5) == deps


def test_zscore_on_synthetic_step(conn):
    """Ten windows at 60, then one at 90, with deliberate variance."""
    counts = [60, 61, 59, 60, 62, 58, 60, 61, 59, 60, 90]
    build_run(conn, "r1", [w(c) for c in counts])
    s = query.series(conn, ["r1"], "post.create")
    deps = derive.rolling_departures(s, n=10, min_samples=5)
    last = deps[-1]
    assert last.baseline_n == 10
    assert last.baseline_mean == pytest.approx(sum(counts[:10]) / 10 / 60)
    assert last.z is not None and last.z > 3
    assert last.condition == "surging"
    assert last.pct_change == pytest.approx(0.5, rel=0.02)


def test_baseline_skips_ineligible_windows(conn):
    """A degraded window neither contributes to nor breaks the baseline."""
    build_run(conn, "r1", [
        *[w(60) for _ in range(6)],
        w(6000, coverage_state="degraded", gate_reasons="low_eps"),  # wild
        w(60),
    ])
    s = query.series(conn, ["r1"], "post.create")
    deps = derive.rolling_departures(s, n=10, min_samples=5)
    assert deps[-1].baseline_mean == pytest.approx(1.0), (
        "the degraded outlier must not enter the baseline"
    )
    assert deps[-1].baseline_n == 6


def test_baseline_skips_partial_windows(conn):
    build_run(conn, "r1", [
        *[w(60) for _ in range(6)],
        w(5, observed_us=5_000_000),  # partial, same 1/s rate
        w(60),
    ])
    s = query.series(conn, ["r1"], "post.create")
    deps = derive.rolling_departures(s, n=10, min_samples=5)
    assert deps[-1].baseline_n == 6


def test_lagged_windows_remain_eligible(conn):
    """Replay backlog is complete data; excluding it would discard most of a
    resumed run for no reason."""
    build_run(conn, "r1", [
        *[w(60, coverage_state="degraded", gate_reasons="lag_high")
          for _ in range(6)],
        w(60),
    ])
    s = query.series(conn, ["r1"], "post.create")
    deps = derive.rolling_departures(s, n=10, min_samples=5)
    assert deps[-1].baseline_n == 6
    assert deps[-1].baseline_mean == pytest.approx(1.0)


def test_no_interpolation_across_unobserved_time(conn):
    """An unobserved window gets a row with no value and teaches nothing."""
    build_run(conn, "r1", [
        *[w(60) for _ in range(6)],
        None,            # UNOBSERVED
        w(120),
    ])
    s = query.series(conn, ["r1"], "post.create")
    deps = derive.rolling_departures(s, n=10, min_samples=5)

    hole = deps[6]
    assert hole.value is None
    assert hole.condition == "unknown"
    assert hole.quality == "unobserved"

    after = deps[7]
    assert after.baseline_n == 6, "the hole contributed nothing"
    assert after.baseline_mean == pytest.approx(1.0)
    assert after.value == pytest.approx(2.0)


def test_every_point_gets_a_departure_row(conn):
    build_run(conn, "r1", [w(60), None, w(60)])
    s = query.series(conn, ["r1"], "post.create")
    assert len(derive.rolling_departures(s)) == len(s.points) == 3


# --- ratio series ----------------------------------------------------------

def test_ratio_series_aligns_by_bucket_and_guards_zero(conn):
    build_run(conn, "r1", [
        {"metrics": {"post.create": 100, "post.create.reply": 50}},
        {"metrics": {"post.create": 0, "post.create.reply": 0}},
        {"metrics": {"post.create": 40, "post.create.reply": 10}},
    ])
    a = query.series(conn, ["r1"], "post.create.reply")
    b = query.series(conn, ["r1"], "post.create")
    pts = derive.ratio_series(a, b)
    assert [p.value for p in pts] == [0.5, None, 0.25]
    assert pts[1].observed is True, "zero denominator is observed, just uncomputable"


def test_ratio_series_marks_unobserved(conn):
    build_run(conn, "r1", [
        {"metrics": {"post.create": 10, "post.create.reply": 5}},
        None,   # UNOBSERVED, inside the observed span
        {"metrics": {"post.create": 10, "post.create.reply": 5}},
    ])
    a = query.series(conn, ["r1"], "post.create.reply")
    b = query.series(conn, ["r1"], "post.create")
    pts = derive.ratio_series(a, b)
    assert pts[1].observed is False
    assert pts[1].value is None
    assert pts[1].quality == "unobserved"


def test_ratio_series_refuses_mismatched_runs(conn):
    build_run(conn, "r1", [{"metrics": {"post.create": 1}}], start_index=0,
              started_at="2026-01-01T00:00:00+00:00",
              ended_at="2026-01-01T00:01:00+00:00")
    build_run(conn, "r2", [{"metrics": {"post.create": 1}}], start_index=9,
              started_at="2026-01-01T00:09:00+00:00",
              ended_at="2026-01-01T00:10:00+00:00")
    a = query.series(conn, ["r1"], "post.create")
    b = query.series(conn, ["r2"], "post.create")
    with pytest.raises(ValueError, match="same runs"):
        derive.ratio_series(a, b)


# --- correlation -----------------------------------------------------------

def test_pearson_exact():
    assert derive.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert derive.pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert derive.pearson([1, 1, 1], [1, 2, 3]) is None  # zero variance
    assert derive.pearson([1, 2], [1, 2]) is None        # too few points


def test_lagged_correlation_finds_a_planted_lead(conn):
    """b copies a shifted one window later; lag +1 should be strongest."""
    pattern = [10, 40, 20, 60, 30, 70, 15, 55, 25, 65, 35, 75, 45, 5, 50]
    windows = []
    for i, v in enumerate(pattern):
        lead = pattern[i - 1] if i > 0 else 0
        windows.append({"metrics": {"post.create.quote": v, "block.create": lead}})
    build_run(conn, "r1", windows)
    a = query.series(conn, ["r1"], "post.create.quote")
    b = query.series(conn, ["r1"], "block.create")
    result = derive.lagged_correlation(a, b, max_lag=3, min_pairs=8)
    best = max((l for l, r in result.items() if r is not None),
               key=lambda l: result[l])
    assert best == 1, result
    assert result[1] == pytest.approx(1.0, abs=1e-6)


def test_lagged_correlation_ignores_ineligible_windows(conn):
    build_run(conn, "r1", [
        {"metrics": {"post.create": 10, "like.create": 10}},
        {"metrics": {"post.create": 99, "like.create": 99},
         "coverage_state": "degraded", "gate_reasons": "low_eps"},
    ])
    a = query.series(conn, ["r1"], "post.create")
    b = query.series(conn, ["r1"], "like.create")
    result = derive.lagged_correlation(a, b, max_lag=1, min_pairs=2)
    assert all(v is None for v in result.values()), (
        "only one eligible pair remains, below min_pairs"
    )


# --- privacy ---------------------------------------------------------------

def test_derived_outputs_contain_no_identity_shaped_fields(conn):
    import re
    build_run(conn, "r1", [w(60) for _ in range(8)])
    s = query.series(conn, ["r1"], "post.create")
    blob = repr(derive.rolling_departures(s))
    blob += repr(derive.ratio_series(s, s))
    blob += repr(derive.lagged_correlation(s, s))
    for pat in (r"did:[a-z]+:", r"at://", r"https?://[a-z]", r"bafy[a-z0-9]{8}"):
        assert not re.search(pat, blob), f"{pat} leaked into derived output"


def test_aggregate_rate_uses_observed_seconds(conn):
    build_run(conn, "r1", [
        {"metrics": {"post.create": 60, "like.create": 120}},
        {"metrics": {"post.create": 60, "like.create": 120}},
    ])
    a = query.series(conn, ["r1"], "post.create")
    b = query.series(conn, ["r1"], "like.create")
    assert derive.aggregate_rate([a, b]) == pytest.approx(360 / 120)
