"""Climatology: does 'normal' have an honest denominator?"""

from __future__ import annotations

import pytest

from weatherwatch import query
from weatherwatch.social.field import climatology as clim_mod
from weatherwatch.social.field import run
from weatherwatch.social.field.quantities import QUANTITY_NAMES

from .conftest import ENDPOINT, write_hourly_run


def _build(conn, days=30, **kw):
    write_hourly_run(conn, "run-a", n_days=days, **kw)
    runs = query.compatible_runs(conn, ENDPOINT)
    return run.build_all(conn, runs, endpoint=ENDPOINT)


# --- primitives -------------------------------------------------------------

def test_percentile_interpolates():
    xs = [1, 2, 3, 4, 5]
    assert clim_mod.percentile(xs, 0.0) == 1
    assert clim_mod.percentile(xs, 0.5) == 3
    assert clim_mod.percentile(xs, 1.0) == 5
    assert clim_mod.percentile([], 0.5) is None
    assert clim_mod.percentile([7], 0.9) == 7


def test_effective_n_shrinks_with_autocorrelation():
    assert clim_mod.effective_n(1000, 0.0) == pytest.approx(1000)
    assert clim_mod.effective_n(1000, 0.9) == pytest.approx(1000 * 0.1 / 1.9)
    assert clim_mod.effective_n(1000, 0.9) < 1000
    assert clim_mod.effective_n(10, 0.999) >= 1.0, "never below one"


def test_effective_n_never_exceeds_the_sample_it_came_from():
    """(1-r)/(1+r) exceeds 1 for negative r, so an alternating series claims
    more independent samples than observations. Seen live: acceleration at
    r=-0.37, n=17,273 reported n_eff 37,576."""
    assert clim_mod.effective_n(1000, -0.37) == 1000
    assert clim_mod.effective_n(1000, -0.9) == 1000
    assert clim_mod.effective_n(17_273, -0.37) <= 17_273


def test_a_negatively_correlated_quantity_reports_n_eff_at_most_n(field_conn):
    _, clim, _, _ = _build(field_conn, days=30)
    for name, q in clim.quantities.items():
        d = q.overall
        for label, val in (("raw", d.n_eff), ("residual", d.n_eff_residual)):
            if val is not None:
                assert val <= d.n + 1e-6, f"{name} {label} n_eff {val} > n {d.n}"


def test_autocorrelation_of_a_trend_is_high():
    r = clim_mod.lag1_autocorrelation([float(i) for i in range(50)])
    assert r is not None and r > 0.9


# --- the two sample sizes ---------------------------------------------------

def test_residual_autocorrelation_is_reported_and_used(field_conn):
    """The raw series is dominated by the daily cycle; conditioning happens
    inside an hour cell, so the residual is the honest independence measure."""
    _, clim, _, _ = _build(field_conn, days=30)
    d = clim.quantities["interaction_velocity"].overall
    assert d.lag1_r > 0.9, "raw series carries the diurnal cycle"
    assert abs(d.lag1_r_residual) < 0.5, "deseasonalised residual does not"
    assert d.n_eff_residual > d.n_eff * 10
    assert clim.quantities["interaction_velocity"].support_note.count("raw") == 1


def test_support_scales_with_day_replicates(field_conn, tmp_path):
    from weatherwatch import db
    seen = {}
    for days, label in ((3, "short"), (8, "medium"), (30, "long")):
        c = db.connect(tmp_path / f"w{days}.sqlite")
        db.init_db(c)
        write_hourly_run(c, "run-a", n_days=days)
        runs = query.compatible_runs(c, ENDPOINT)
        _, clim, _, _ = run.build_all(c, runs, endpoint=ENDPOINT)
        seen[label] = clim.quantities["interaction_velocity"].support
        c.close()
    assert seen["short"] == clim_mod.UNSUPPORTED
    assert seen["medium"] == clim_mod.THIN
    assert seen["long"] == clim_mod.SUPPORTED


def test_hour_of_week_is_computed_as_unsupported_not_used(field_conn):
    _, clim, _, _ = _build(field_conn, days=30)
    assert clim.n_weeks < clim_mod.MIN_WEEKS_FOR_HOUR_OF_WEEK
    assert clim.hour_of_week_supported is False
    assert "independent *weeks*" in clim.hour_of_week_note


def test_diurnal_cells_count_day_replicates(field_conn):
    _, clim, _, _ = _build(field_conn, days=30)
    cells = clim.quantities["interaction_velocity"].diurnal
    assert len(cells) == 24
    assert all(c.n_days >= 25 for c in cells)
    # a real daily cycle must be visible, or conditioning by hour is pointless
    medians = [c.p50 for c in cells]
    assert max(medians) > 1.5 * min(medians)


# --- holes ------------------------------------------------------------------

def test_unobserved_windows_are_never_filled(field_conn):
    missing = {50, 51, 52, 53}
    points, clim, obs, _ = _build(field_conn, days=30, unobserved=missing)
    starts = {p.bucket_start for p in points}
    assert len(starts) == len(points)
    unobserved = [p for p in points if not p.observed]
    assert len(unobserved) == 0 or all(
        all(v is None for v in p.values.values()) for p in unobserved)
    # the climatology counts only what was eligible
    assert clim.n_eligible <= clim.n_windows


def test_a_gap_does_not_become_a_zero(field_conn):
    points, clim, obs, _ = _build(field_conn, days=30, unobserved={100})
    for o in obs:
        for name, value in o.metrics.items():
            assert value is not None
    # nothing in the climatology may be zero purely because nobody watched
    d = clim.quantities["interaction_velocity"].overall
    assert d.minimum > 0


# --- candidates are not detections -----------------------------------------

def test_unsupported_quantities_produce_no_candidates(field_conn):
    _, clim, _, cands = _build(field_conn, days=3)
    assert all(q.support == clim_mod.UNSUPPORTED
               for q in clim.quantities.values())
    assert cands == []


def test_degenerate_cells_do_not_flag_everything(field_conn):
    """A near-constant quantity has p05 == p95; without a spread guard every
    window with float jitter reads as unusual. Observed: 192 of 192."""
    points, clim, _, cands = _build(field_conn, days=30)
    flat = clim_mod.DiurnalCell(hour=0, n=30, n_days=30, p05=1.0, p25=1.0,
                                p50=1.0, p75=1.0, p95=1.0)
    level = max(abs(flat.p50), 1e-9)
    assert (flat.p95 - flat.p05) <= clim_mod.DEGENERATE_SPREAD_FRACTION * level
    # and the real run is nowhere near flagging everything
    summary = clim_mod.candidate_summary(points, clim, cands)
    assert summary["observed_rate"] < 0.30


def test_candidate_rate_is_reported_against_its_nominal(field_conn):
    """"856 candidates" reads as 856 anomalies. It is mostly the definition."""
    points, clim, _, cands = _build(field_conn, days=30)
    s = clim_mod.candidate_summary(points, clim, cands)
    assert s["nominal_rate"] == 0.10
    assert s["observed_rate"] == pytest.approx(
        s["candidates"] / s["scored_pairs"], rel=1e-3)
    assert s["excess_over_nominal"] == pytest.approx(
        s["observed_rate"] - 0.10, abs=1e-6)
    assert "by construction" in s["note"]


def test_a_real_spike_is_flagged_above_the_background(field_conn):
    """The instrument must still notice something genuinely unusual."""
    idx = 24 * 20 + 13
    points, clim, _, cands = _build(field_conn, days=30, spike={idx: 6.0})
    spiked = [c for c in cands
              if c.bucket_start == points[0].bucket_start + idx * 3600]
    assert spiked, "a 6x hour should sit outside its own hour cell"
    by_q = {c.quantity: c for c in spiked}
    assert by_q["interaction_velocity"].direction == "above"
    assert by_q["emission_velocity"].direction == "above"
    assert all("not a finding" in c.note for c in spiked)
    # A uniform spike leaves ratio quantities alone, so they must NOT be
    # flagged: scaling numerator and denominator together changes nothing.
    assert "interaction_pressure" not in by_q
    assert "reply_share" not in by_q


def test_a_rounding_error_is_not_an_exceedance(field_conn):
    """Being outside a band by 0.002% is not being outside it."""
    points, clim, _, cands = _build(field_conn, days=30,
                                    spike={24 * 20 + 13: 6.0})
    for c in cands:
        span = c.cell_p95 - c.cell_p05
        over = (c.value - c.cell_p95) if c.direction == "above" else (
            c.cell_p05 - c.value)
        assert over > clim_mod.MIN_EXCEEDANCE_FRACTION * span * 0.999


def test_climatology_id_is_content_addressed(field_conn):
    _, clim, _, _ = _build(field_conn, days=30)
    again = clim_mod.build(
        [], window=clim.window)  # different content -> different id
    assert clim.climatology_id != again.climatology_id
    assert clim.as_dict()["climatology_id"] == clim.climatology_id


def test_every_quantity_appears_in_the_climatology(field_conn):
    _, clim, _, _ = _build(field_conn, days=30)
    assert set(clim.quantities) == set(QUANTITY_NAMES)


def test_candidate_rate_is_none_when_nothing_was_scored(field_conn):
    """0.0 would read as "well below normal"; it means "not measured"."""
    points, clim, _, cands = _build(field_conn, days=3)
    s = clim_mod.candidate_summary(points, clim, cands)
    assert s["scored_pairs"] == 0
    assert s["observed_rate"] is None
    assert s["excess_over_nominal"] is None
