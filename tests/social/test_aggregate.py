"""Tier 1: episodes from the buckets weatherwatch already persists."""

from __future__ import annotations

from weatherwatch.social.scope import Scope
from weatherwatch.social.sensors import aggregate

from .conftest import build_run, steady_then_burst

RUN = "run-agg"
EP = "wss://relay-a.invalid/subscribe"


def _evidence(conn, windows, metric="block.create"):
    build_run(conn, RUN, windows)
    return aggregate.select(conn, [RUN], metric, endpoint=EP)


def test_repeated_departure_windows_form_one_episode(conn):
    ev = _evidence(conn, steady_then_burst(n_burst=4))
    found = aggregate.interpret(ev, aggregate.AggregateConfig(detect_lulls=False))
    bursts = [f for f in found if f.type == "block_burst"]
    assert len(bursts) == 1
    assert bursts[0].explain["n_windows"] >= 4
    assert bursts[0].explain["direction"] == "excess"


def test_metric_names_map_to_readable_episode_types(conn):
    ev = _evidence(conn, steady_then_burst(metric="like.create"),
                   metric="like.create")
    found = aggregate.interpret(ev, aggregate.AggregateConfig(detect_lulls=False))
    assert {f.type for f in found} == {"like_storm"}


def test_two_separated_bursts_do_not_merge(conn):
    """The load-bearing negative. Quiet between two excursions means two
    episodes; merging them would invent a continuous event."""
    windows = steady_then_burst(n_burst=3, n_tail=10)
    windows += [{"metrics": {"block.create": 90}} for _ in range(3)]
    windows += [{"metrics": {"block.create": 10}} for _ in range(4)]
    ev = _evidence(conn, windows)
    found = [f for f in aggregate.interpret(
        conn and ev, aggregate.AggregateConfig(detect_lulls=False))
        if f.type == "block_burst"]
    assert len(found) == 2
    assert found[0].ts_end <= found[1].ts_start
    assert found[0].episode_id != found[1].episode_id


def test_short_gap_inside_one_burst_does_not_split_it(conn):
    windows = [{"metrics": {"block.create": 10 + (i % 3) - 1}} for i in range(20)]
    windows += [{"metrics": {"block.create": 90}} for _ in range(3)]
    windows += [{"metrics": {"block.create": 60}}]          # dips, still high
    windows += [{"metrics": {"block.create": 90}} for _ in range(3)]
    windows += [{"metrics": {"block.create": 10}} for _ in range(5)]
    ev = _evidence(conn, windows)
    found = [f for f in aggregate.interpret(
        ev, aggregate.AggregateConfig(detect_lulls=False))
        if f.type == "block_burst"]
    assert len(found) == 1


def test_flat_series_produces_no_episodes(conn):
    windows = [{"metrics": {"block.create": 10 + (i % 3) - 1}} for i in range(40)]
    ev = _evidence(conn, windows)
    assert aggregate.interpret(ev) == []


def test_lull_is_detected_as_its_own_type(conn):
    """Withdrawal is an event: an instrument that only sees surges is blind
    to a network going quiet."""
    windows = [{"metrics": {"block.create": 100 + (i % 5)}} for i in range(20)]
    windows += [{"metrics": {"block.create": 0}} for _ in range(4)]
    windows += [{"metrics": {"block.create": 100}} for _ in range(4)]
    ev = _evidence(conn, windows)
    found = aggregate.interpret(ev)
    assert any(f.type == "block_lull" for f in found)
    assert all(f.explain["direction"] == "deficit"
               for f in found if f.type == "block_lull")


def test_unobserved_windows_do_not_become_zeros(conn):
    """An unobserved stretch must not read as a collapse to zero."""
    windows = [{"metrics": {"block.create": 100 + (i % 5)}} for i in range(20)]
    windows += [None, None, None, None]          # nobody was watching
    windows += [{"metrics": {"block.create": 100}} for _ in range(4)]
    ev = _evidence(conn, windows)
    found = aggregate.interpret(ev)
    assert not any(f.type == "block_lull" for f in found)
    assert ev.facts["n_unobserved"] == 4


def test_evidence_covers_the_whole_interval_not_just_the_episode(conn):
    """Receipts commit to the windows that supported nothing, too — which is
    what makes cherry-picking detectable on replay."""
    windows = steady_then_burst(n_burst=4)
    ev = _evidence(conn, windows)
    found = aggregate.interpret(ev, aggregate.AggregateConfig(detect_lulls=False))
    assert len(ev.receipts) == len(windows)
    assert len(found[0].segment_receipts) < len(ev.receipts)
    assert set(found[0].segment_receipts) <= set(ev.receipts)


def test_scope_carries_no_thresholds(conn):
    """If a threshold ever lands on Scope, evidence identity stops being
    config-independent. Guarded by field set, not by good intentions."""
    ev = _evidence(conn, steady_then_burst())
    assert set(Scope.__dataclass_fields__) == {
        "kind", "subject_class", "ts_start", "ts_end", "window", "source"}
    assert ev.scope.kind == "aggregate"
    assert ev.scope.subject_class == "block.create"


def test_mean_baseline_is_contaminated_by_the_first_burst(conn):
    """Why `baseline="robust"` is the default, stated as a test rather than a
    comment. The mean path is kept for reproducing the weather lane's own
    numbers; it is not a safe default for episode detection."""
    windows = steady_then_burst(n_burst=3, n_tail=10)
    windows += [{"metrics": {"block.create": 90}} for _ in range(3)]
    windows += [{"metrics": {"block.create": 10}} for _ in range(4)]
    ev = _evidence(conn, windows)

    mean_found = [f for f in aggregate.interpret(
        ev, aggregate.AggregateConfig(baseline="mean", detect_lulls=False))
        if f.type == "block_burst"]
    robust_found = [f for f in aggregate.interpret(
        ev, aggregate.AggregateConfig(baseline="robust", detect_lulls=False))
        if f.type == "block_burst"]

    assert len(mean_found) == 1, "mean baseline loses the second burst"
    assert len(robust_found) == 2, "median baseline keeps both"


def test_magnitude_is_a_doubling_scale_not_a_z_score(conn):
    """Significance decides whether there is an episode; effect size decides
    how big it is. Conflating them made a 1.03x like rate read as critical."""
    from weatherwatch.social.scope import magnitude, magnitude_band

    assert magnitude(1.0) == 0.0
    assert magnitude(2.0) == 1.0
    assert magnitude(4.0) == 2.0
    assert magnitude(0.5) == 0.0, "sub-null ratios floor at zero, not negative"

    ev = _evidence(conn, steady_then_burst(baseline_count=10, burst_count=90))
    f = aggregate.interpret(ev, aggregate.AggregateConfig(detect_lulls=False))[0]
    assert f.explain["rate_ratio"] > 5
    assert f.score == magnitude(f.explain["rate_ratio"])
    assert magnitude_band(f.score) in {"med", "high", "critical"}


def test_a_statistically_large_but_tiny_departure_reads_small(conn):
    """The real-data failure this scale exists for: a 3% rate change on a very
    smooth series clears any z gate and is still a 3% rate change."""
    windows = [{"metrics": {"like.create": 1000 + (i % 3) - 1}} for i in range(20)]
    windows += [{"metrics": {"like.create": 1030}} for _ in range(4)]
    windows += [{"metrics": {"like.create": 1000}} for _ in range(4)]
    ev = _evidence(conn, windows, metric="like.create")
    found = [f for f in aggregate.interpret(
        ev, aggregate.AggregateConfig(detect_lulls=False))
        if f.type == "like_storm"]
    assert found, "still detected as an episode"
    assert found[0].explain["peak_z"] > 3.0
    assert found[0].score < 0.585
    assert found[0].explain["rate_ratio"] < 1.1


def test_a_lull_is_measured_at_its_trough_not_its_peak(conn):
    """Taking max() in both directions reported every lull as magnitude 0."""
    windows = [{"metrics": {"block.create": 100 + (i % 5)}} for i in range(20)]
    windows += [{"metrics": {"block.create": 10}} for _ in range(4)]
    windows += [{"metrics": {"block.create": 100}} for _ in range(4)]
    ev = _evidence(conn, windows)
    lull = [f for f in aggregate.interpret(ev) if f.type == "block_lull"][0]
    assert lull.explain["rate_ratio"] > 5
    assert lull.score > 2.0
    assert lull.explain["extreme_rate_eps"] < lull.explain["baseline_rate_eps"]


def test_bounds_are_converted_and_aligned(conn):
    """Regression, found on the deployed database: seven days of history
    returned zero episodes in 0.13s.

    `bucket_start` is unix seconds; the social lane speaks Jetstream
    microseconds. Passing microseconds straight through put the range off the
    end of the table. Aligning matters just as much: `query.series` steps its
    densified range by `width` from whatever bound it is handed, so an
    unaligned start makes every step land between buckets and the series
    densifies to all-unobserved -- zero episodes, no error, no clue.
    """
    from weatherwatch import timeutil
    from .conftest import SYNTH_BASE, SYNTH_WIDTH

    windows = steady_then_burst(n_burst=4)
    build_run(conn, RUN, windows)
    base_us = SYNTH_BASE * 1_000_000
    span_us = len(windows) * SYNTH_WIDTH * 1_000_000

    ev = aggregate.select(conn, [RUN], "block.create",
                          base_us, base_us + span_us, endpoint=EP)
    assert ev.facts["n_windows"] == len(windows)
    assert ev.facts["n_observed"] == len(windows)
    assert ev.facts["n_unobserved"] == 0
    assert aggregate.interpret(
        ev, aggregate.AggregateConfig(detect_lulls=False))

    # A start 17 seconds into a 60s bucket must still land on the bucket.
    off = aggregate.select(conn, [RUN], "block.create",
                           base_us + 17_000_000, base_us + span_us,
                           endpoint=EP)
    assert off.facts["n_unobserved"] == 0
    assert off.facts["n_windows"] == len(windows)

    # And a genuinely later range must exclude the earlier windows.
    half = base_us + span_us // 2
    later = aggregate.select(conn, [RUN], "block.create", half,
                             base_us + span_us, endpoint=EP)
    assert 0 < later.facts["n_windows"] < len(windows)
    assert later.facts["n_unobserved"] == 0


def test_microsecond_bounds_would_have_selected_nothing(conn):
    """Pins the failure mode itself, so the fix cannot be quietly reverted."""
    from .conftest import SYNTH_BASE
    from weatherwatch import query as _q

    build_run(conn, RUN, steady_then_burst(n_burst=4))
    base_us = SYNTH_BASE * 1_000_000
    raw = _q.series(conn, [RUN], "block.create", since=base_us,
                    until=base_us + 60_000_000)
    assert all(not p.observed for p in raw.points), \
        "microsecond bounds land off the end of a seconds-keyed table"
