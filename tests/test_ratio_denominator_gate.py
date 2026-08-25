"""Extremes must not be drawn from denominators too small to mean anything.

Kimi, reviewing the published page cold: *disclaimers don't travel with
screenshots*. `block/follow = 22.75` is correct arithmetic and reads as
"BLOCKING UP 2200%" once cropped. The guard therefore has to be structural.
"""

from __future__ import annotations

import re

from weatherwatch import report

from .conftest import build_run


def _render(conn, tmp_path, windows):
    build_run(conn, "run-ratio", windows)
    report.generate_report(conn, tmp_path / "site")
    return (tmp_path / "site" / "index.html").read_text()


def test_a_tiny_denominator_cannot_produce_a_headline_extreme(conn, tmp_path):
    """One window with four follows and ninety blocks must not set the max."""
    windows = [{"metrics": {"block.create": 40 + (i % 3),
                            "follow.create": 400 + (i % 5)}} for i in range(20)]
    windows.append({"metrics": {"block.create": 90, "follow.create": 4}})
    html = _render(conn, tmp_path, windows)

    row = next(ln for ln in html.splitlines() if "block/follow" in ln)
    cell = row.split("block/follow")[1]
    # 90/4 = 22.5 must not appear as the window extreme.
    assert "22.5" not in cell, "an extreme was taken from a 4-event denominator"
    # Substring care: "404 follow.create" contains "4 follow.create", so match
    # the whole expression rather than a fragment of it.
    assert "90 block.create / 4 follow.create" not in cell
    assert ">1</td>" in cell, "the excluded window must be counted"


def test_excluded_windows_are_counted_not_dropped(conn, tmp_path):
    windows = [{"metrics": {"block.create": 40, "follow.create": 400}}
               for _ in range(20)]
    windows += [{"metrics": {"block.create": 9, "follow.create": 3}}
                for _ in range(4)]
    html = _render(conn, tmp_path, windows)
    assert "windows too thin" in html
    assert "Extremes are drawn only from windows whose" in html
    assert f"denominator reached {report.MIN_RATIO_DENOMINATOR}" in html


def test_healthy_denominators_still_produce_extremes(conn, tmp_path):
    windows = [{"metrics": {"block.create": 40 + i,
                            "follow.create": 400 - i}} for i in range(20)]
    html = _render(conn, tmp_path, windows)
    row = next(ln for ln in html.splitlines() if "block/follow" in ln)
    assert "—" not in row.split("block/follow")[1][:200], \
        "a well-populated ratio must still report its extremes"


def test_the_floor_is_a_named_constant_not_a_magic_number():
    assert isinstance(report.MIN_RATIO_DENOMINATOR, int)
    assert report.MIN_RATIO_DENOMINATOR >= 10


# --- the condition label needs effect size, not only significance ----------

def test_a_significant_but_tiny_change_is_not_labelled_surging():
    """The published page showed `surging` at z = 16.45. A trailing baseline
    with almost no variance makes a 3% change enormously significant, and a
    reader cannot un-see a red word."""
    from weatherwatch import derive

    assert derive.condition(16.45) == derive.COND_SURGING, "z alone still does"
    assert derive.condition(16.45, pct_change=0.03) == derive.COND_NORMAL
    assert derive.condition(16.45, pct_change=0.9) == derive.COND_SURGING
    assert derive.condition(-9.0, pct_change=0.01) == derive.COND_NORMAL
    assert derive.condition(-9.0, pct_change=-0.8) == derive.COND_DEGRADING


def test_the_effect_gate_does_not_touch_any_reported_number(conn, tmp_path):
    """Only the coloured word is gated; z, baseline and percent are unchanged."""
    from weatherwatch import derive, query

    windows = [{"metrics": {"post.create": 1000 + (i % 3)}} for i in range(20)]
    windows += [{"metrics": {"post.create": 1030}} for _ in range(3)]
    build_run(conn, "run-eff", windows)
    s = query.series(conn, ["run-eff"], "post.create")
    deps = derive.rolling_departures(s)
    last = next(d for d in reversed(deps) if d.z is not None)

    # Large enough that it WOULD have been labelled on z alone...
    assert abs(last.z) >= derive.Z_ELEVATED
    assert derive.condition(last.z) != derive.COND_NORMAL
    # ...but the change itself is materially nothing.
    assert abs(last.pct_change) < derive.MIN_LABEL_EFFECT
    assert last.condition == derive.COND_NORMAL
    # and every reported number survives untouched
    assert last.baseline_mean is not None and last.value is not None
    assert last.z is not None and last.pct_change is not None


def test_the_floor_does_not_claim_statistical_authority(conn, tmp_path):
    """A threshold presented as derived would replace one false authority
    with another. The instruction for this guard was explicit: if a threshold
    cannot be justified, say so rather than implying it can."""
    windows = [{"metrics": {"block.create": 40, "follow.create": 400}}
               for _ in range(20)]
    html = " ".join(_render(conn, tmp_path, windows).split())
    assert "legibility floor, not a statistical one" in html
    assert "no power calculation" in html


def test_both_lanes_agree_on_what_a_condition_word_means(conn):
    """`elevated` must not depend on which module computed it.

    The social lane runs its own robust (median/MAD) departure estimator over
    the same series. Different estimator, same vocabulary — so the effect-size
    floor has to apply on both sides or one word carries two meanings.
    """
    from weatherwatch import derive, query
    from weatherwatch.social.sensors import aggregate

    # A near-flat baseline, then a change too small for a reader to call one.
    windows = [{"metrics": {"post.create": 1000 + (i % 3)}} for i in range(20)]
    windows += [{"metrics": {"post.create": 1030}} for _ in range(3)]
    build_run(conn, "run-lanes", windows)
    s = query.series(conn, ["run-lanes"], "post.create")

    cfg = aggregate.AggregateConfig()
    robust = [d for d in aggregate.robust_departures(
        s, n=cfg.baseline_n, min_samples=cfg.min_baseline) if d.z is not None]
    last = robust[-1]
    assert abs(last.z) >= derive.Z_ELEVATED, "z alone would have labelled it"
    assert abs(last.pct_change) < derive.MIN_LABEL_EFFECT
    assert last.condition == derive.COND_NORMAL, (
        "the social aggregate lane must apply the same effect-size gate as "
        "the report lane")
