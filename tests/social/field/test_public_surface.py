"""Qualification for the public instrument: the campaign's six requirements.

Success condition, restated as tests: a visitor can see that something is
happening; an expert can see that nobody is pretending to know why.
"""

from __future__ import annotations

import json
import re

import pytest

from weatherwatch import query
from weatherwatch.social.field import baseline, conditions as cond_mod
from weatherwatch.social.field import observation as obs_mod
from weatherwatch.social.field import run, viz
from weatherwatch.social.field.climatology import UNSUPPORTED, candidate_summary

from .conftest import ENDPOINT, write_hourly_run

IDENTITY_RE = re.compile(
    r"did:(plc|web|key):|at://|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)|\ba:[0-9a-f]{12}\b")

GEO_TOKENS = ("latitude", "longitude", "geoip", "globe", "world map",
              "country", "continent", "region of", "ip address", "pds location")

CAUSAL = ("caused by", "because of", "responsible for", "to blame", "culprit",
          "guilty", "perpetrator", "instigator")
ACCUSATORY = ("toxic", "harass", "brigad", "abusive", "bad actor", "offender",
              "malicious", "coordinated abuse", "hostile user")


def _build(conn, days=30, **kw):
    write_hourly_run(conn, "run-a", n_days=days, **kw)
    runs = query.compatible_runs(conn, ENDPOINT)
    points, clim, obs, cands = run.build_all(conn, runs, endpoint=ENDPOINT)
    docs = [o.as_dict() for o in obs]
    cdoc = clim.as_dict()
    cond = cond_mod.assess(docs, cdoc).as_dict()
    # The production builder, not a re-implementation of it: a test that
    # renders a shape the CLI never produces qualifies the wrong page.
    cond["criteria_table"] = cond_mod.criteria_table()
    return points, clim, cdoc, docs, cond, cands


@pytest.fixture()
def storm(field_conn):
    total = 30 * 24
    return _build(field_conn, days=30,
                  spike={total - 1 - i: 4.5 for i in range(4)})


@pytest.fixture()
def public_html(storm):
    _, _, cdoc, docs, cond, _ = storm
    return viz.render_public(docs, cdoc, {"generated_at": "t"}, cond)


@pytest.fixture()
def station_html(storm):
    _, _, cdoc, docs, cond, _ = storm
    return viz.render_station(docs, cdoc, {"generated_at": "t"}, cond)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", html).lower()


# --- 1. unavailable data never becomes calm --------------------------------

def test_thin_history_never_reads_as_calm(field_conn):
    _, _, cdoc, docs, cond, _ = _build(field_conn, days=3)
    assert cond["state"] == cond_mod.UNAVAILABLE
    html = _text(viz.render_public(docs, cdoc, {"generated_at": "t"}, cond))
    assert "calm" not in html.split("what this instrument cannot see")[0] \
        or "not a reading of calm" in html


def test_missing_data_never_becomes_calm(field_conn):
    """Every window ineligible: the page must say it cannot tell."""
    _, _, cdoc, docs, _, _ = _build(field_conn, days=30)
    for d in docs:
        d["confidence"] = dict(d["confidence"], eligible=False)
    c = cond_mod.assess(docs, cdoc)
    assert c.state == cond_mod.UNAVAILABLE
    assert c.state != cond_mod.CALM
    assert "not a reading of calm" in c.confidence_plain


def test_unknown_is_a_valid_result_with_its_own_label():
    assert cond_mod.STATE_LABEL[cond_mod.UNAVAILABLE] == "Conditions unavailable"
    assert cond_mod.UNAVAILABLE in dict(cond_mod.CRITERIA)


# --- 2. public states match documented criteria ----------------------------

def test_every_state_has_criteria_and_they_are_rendered(public_html):
    documented = dict(cond_mod.CRITERIA)
    assert set(documented) == set(cond_mod.STATE_LABEL)
    for text in documented.values():
        assert text[:40] in public_html


def test_the_state_shown_carries_the_rule_that_produced_it(storm):
    _, _, _, _, cond, _ = storm
    assert cond["criteria"] == dict(cond_mod.CRITERIA)[cond["state"]]


def test_state_mapping_is_deterministic(field_conn):
    total = 30 * 24
    spike = {total - 1 - i: 4.5 for i in range(4)}
    a = _build(field_conn, days=30, spike=spike)[4]
    write_hourly_run.__doc__  # no-op; second build over the same store
    runs = query.compatible_runs(field_conn, ENDPOINT)
    _, clim2, obs2, _ = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    b = cond_mod.assess([o.as_dict() for o in obs2], clim2.as_dict()).as_dict()
    assert a["state"] == b["state"]
    assert a["criteria"] == b["criteria"]
    assert a["persistence_windows"] == b["persistence_windows"]


# --- 3. identity never reaches the public projection -----------------------

def test_public_page_carries_no_identifier(public_html):
    assert not IDENTITY_RE.search(public_html)


def test_station_page_carries_no_identifier(station_html):
    assert not IDENTITY_RE.search(station_html)


def test_conditions_payload_carries_no_identifier(storm):
    _, _, _, _, cond, _ = storm
    assert not IDENTITY_RE.search(json.dumps(cond, sort_keys=True))


# --- 4. no geographic fields exist -----------------------------------------

def test_no_geographic_language_on_either_page(public_html, station_html):
    for html in (public_html, station_html):
        low = _text(html)
        for token in GEO_TOKENS:
            assert token not in low, f"page mentions {token!r}"


def test_the_absence_of_geography_is_stated_publicly(public_html):
    assert "no geography" in _text(public_html)
    assert "location" in obs_mod.STRUCTURAL_ABSENCES


# --- 5. no causal language in public output --------------------------------

def test_public_output_makes_no_causal_or_accusatory_claim(public_html):
    low = _text(public_html)
    for bad in CAUSAL + ACCUSATORY:
        assert bad not in low, f"public page says {bad!r}"


def test_public_output_states_the_non_causal_posture(public_html):
    low = _text(public_html)
    assert "observation is not causation" in low
    assert "cannot report who caused it" in low


def test_state_names_never_describe_a_person():
    joined = " ".join(cond_mod.STATE_LABEL.values()).lower()
    for bad in ACCUSATORY + ("user", "account", "community"):
        assert bad not in joined


# --- 4b. measurement humility: observed beside not-observed ----------------

def test_every_shown_measurement_is_paired_with_its_limit(storm):
    _, _, _, _, cond, _ = storm
    assert cond["pairings"], "a storm state must show measurements"
    for p in cond["pairings"]:
        assert p["observed"] and p["not_observed"]
        assert p["not_observed"] != p["observed"]


def test_the_page_renders_both_columns(public_html):
    low = _text(public_html)
    assert "observed" in low and "not observed" in low
    assert "that this was coordinated, planned or organised" in low
    assert "that anyone is upset" in low


def test_an_unmapped_quantity_still_gets_a_limit():
    """A measurement shown without its limit is the failure this prevents."""
    r = cond_mod.Reason(plain="Something moved.", quantity="not_in_the_table")
    pairs = cond_mod._pairings([r])
    assert len(pairs) == 1
    assert pairs[0].not_observed == cond_mod.GENERIC_NOT_OBSERVED


# --- 6. replay produces identical observations -----------------------------

def test_replay_reproduces_every_observation_id(storm):
    _, _, _, docs, _, _ = storm
    for d in docs[:200]:
        assert run.replay_observation(d) == d["observation_id"]


def test_public_page_is_byte_identical_on_re_render(storm):
    _, _, cdoc, docs, cond, _ = storm
    meta = {"generated_at": "fixed"}
    assert (viz.render_public(docs, cdoc, meta, cond)
            == viz.render_public(docs, cdoc, meta, cond))


# --- 7. calibration failures are visible -----------------------------------

def test_station_page_announces_incomplete_calibration(field_conn):
    """A thin or unsupported baseline must be stated, not quietly tolerated."""
    _, clim, cdoc, docs, cond, _ = _build(field_conn, days=8)
    supports = {q["support"] for q in cdoc["quantities"].values()}
    assert supports & {"thin", UNSUPPORTED}, "fixture must be under-calibrated"
    html = viz.render_station(docs, cdoc, {"generated_at": "t"}, cond)
    low = _text(html)
    assert "calibration is incomplete" in low
    assert "excluded from candidates" in low


def test_a_fully_calibrated_run_makes_no_failure_claim(field_conn):
    _, _, cdoc, docs, cond, _ = _build(field_conn, days=30)
    assert all(q["support"] == "supported"
               for q in cdoc["quantities"].values())
    html = viz.render_station(docs, cdoc, {"generated_at": "t"}, cond)
    assert "calibration is incomplete" not in _text(html)


def test_unsupported_quantities_are_excluded_from_the_public_state(field_conn):
    _, clim, cdoc, docs, cond, cands = _build(field_conn, days=3)
    assert all(q["support"] == UNSUPPORTED
               for q in cdoc["quantities"].values())
    assert cands == []
    assert cond["state"] == cond_mod.UNAVAILABLE


# --- 8. the public page is not a dashboard ---------------------------------

def test_public_page_exposes_no_raw_metrics_or_jargon(public_html):
    low = _text(public_html)
    for jargon in ("n_eff", "lag-1", "autocorrel", "herfindahl", "meteogram",
                   "coefficient of variation", "ar(1)", "climatology id",
                   "p95", "deseasonalis"):
        assert jargon not in low, f"public page exposes {jargon!r}"


def test_public_page_is_far_smaller_than_the_station(public_html, station_html):
    assert len(public_html) * 5 < len(station_html)


def test_public_page_has_no_tables_of_numbers(public_html):
    assert "<table" not in public_html.split("How conditions are decided")[0]


def test_station_page_declares_itself_operator_facing(station_html):
    low = _text(station_html)
    assert "operator surface, not the public page" in low


# --- 9. the baseline report -------------------------------------------------

def test_baseline_report_answers_what_normal_looks_like(storm):
    points, clim, cdoc, docs, _, cands = storm
    md = baseline.report(cdoc, docs, candidate_summary(points, clim, cands),
                         {"generated_at": "t"})
    for heading in ("What does normal look like?", "What was observed",
                    "How much independent sample", "Seasonality",
                    "What normal looks like",
                    "Conclusions this baseline cannot support",
                    "Blind spots", "Replay"):
        assert heading in md, f"report is missing {heading!r}"


def test_baseline_report_shows_both_sample_sizes(storm):
    points, clim, cdoc, docs, _, cands = storm
    md = baseline.report(cdoc, docs, candidate_summary(points, clim, cands))
    assert "n_eff (raw)" in md and "n_eff (residual)" in md
    assert "the independent replicates are **days**" in md


def test_baseline_report_states_hour_of_week_is_not_used(storm):
    points, clim, cdoc, docs, _, cands = storm
    md = baseline.report(cdoc, docs, candidate_summary(points, clim, cands))
    assert "computed, reported, and not used" in md


def test_baseline_report_refuses_to_approximate_participant_turnover(storm):
    points, clim, cdoc, docs, _, cands = storm
    md = baseline.report(cdoc, docs, candidate_summary(points, clim, cands))
    assert "Participant turnover specifically" in md
    assert "not approximated" in md
    assert "a proxy for an unmeasurable quantity" in md.replace("\n", " ")


def test_baseline_report_carries_no_identifier(storm):
    points, clim, cdoc, docs, _, cands = storm
    md = baseline.report(cdoc, docs, candidate_summary(points, clim, cands))
    assert not IDENTITY_RE.search(md)
