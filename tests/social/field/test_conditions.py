"""The public layer: readable without the machinery, honest about its limits."""

from __future__ import annotations

import re

import pytest

from weatherwatch import query
from weatherwatch.social.field import conditions as cond_mod
from weatherwatch.social.field import run, viz

from .conftest import ENDPOINT, write_hourly_run


def _assess(conn, days=30, **kw):
    write_hourly_run(conn, "run-a", n_days=days, **kw)
    runs = query.compatible_runs(conn, ENDPOINT)
    _, clim, obs, _ = run.build_all(conn, runs, endpoint=ENDPOINT)
    docs = [o.as_dict() for o in obs]
    return cond_mod.assess(docs, clim.as_dict()), docs, clim.as_dict()


# --- vocabulary -------------------------------------------------------------

def test_no_state_names_a_person_or_a_motive():
    joined = " ".join(cond_mod.STATE_LABEL.values()).lower()
    for bad in ("toxic", "abus", "harass", "brigad", "bad actor", "hostile",
                "coordinat", "offender", "malicious", "user", "account",
                "community"):
        assert bad not in joined, f"state vocabulary says {bad!r}"


def test_states_are_the_ones_the_brief_asked_for():
    assert cond_mod.STATE_LABEL[cond_mod.CALM] == "Calm"
    assert cond_mod.STATE_LABEL[cond_mod.ACTIVE] == "Active"
    assert cond_mod.STATE_LABEL[cond_mod.TURBULENT] == "Turbulent"
    assert cond_mod.STATE_LABEL[cond_mod.STORM] == "Storm"
    assert cond_mod.STATE_LABEL[cond_mod.SEVERE] == "Severe storm"


def test_every_state_has_published_criteria():
    documented = {s for s, _ in cond_mod.CRITERIA}
    assert documented == set(cond_mod.STATE_LABEL)
    for _, text in cond_mod.CRITERIA:
        assert len(text) > 30 and text.endswith(".")


# --- honest degradation -----------------------------------------------------

def test_thin_history_yields_unavailable_not_calm(field_conn):
    """"We cannot tell" and "nothing is happening" are different facts, and
    only one of them is reassuring."""
    c, _, _ = _assess(field_conn, days=3)
    assert c.state == cond_mod.UNAVAILABLE
    assert c.state != cond_mod.CALM
    assert "not a reading of calm" in c.confidence_plain
    assert "long enough" in c.plain or "baseline" in c.plain


def test_no_observations_is_unavailable():
    c = cond_mod.assess([], {})
    assert c.state == cond_mod.UNAVAILABLE
    assert c.reasons == ()


def test_supported_history_produces_a_real_state(field_conn):
    c, _, _ = _assess(field_conn, days=30)
    assert c.state != cond_mod.UNAVAILABLE
    assert c.confidence in ("supported", "thin")
    assert c.label in cond_mod.STATE_LABEL.values()


# --- the criteria actually govern ------------------------------------------

def test_a_sustained_spike_escalates_the_state(field_conn):
    """The last windows are pushed far above their hour-typical level."""
    total = 30 * 24
    spike = {total - 1 - i: 5.0 for i in range(4)}
    c, docs, clim = _assess(field_conn, days=30, spike=spike)
    assert c.state in (cond_mod.STORM, cond_mod.SEVERE)
    assert c.persistence_windows >= cond_mod.STORM_PERSISTENCE
    assert "storm" in c.headline.lower()


def test_a_single_spike_window_is_not_a_storm(field_conn):
    """Persistence is what separates a gust from a storm."""
    total = 30 * 24
    c, _, _ = _assess(field_conn, days=30, spike={total - 1: 5.0})
    assert c.persistence_windows < cond_mod.STORM_PERSISTENCE
    assert c.state != cond_mod.STORM


def test_state_carries_the_rule_that_produced_it(field_conn):
    c, _, _ = _assess(field_conn, days=30)
    expected = dict(cond_mod.CRITERIA)[c.state]
    assert c.criteria == expected


def test_persistence_stops_at_the_first_ordinary_window(field_conn):
    total = 30 * 24
    _, docs, clim = _assess(field_conn, days=30,
                            spike={total - 1: 5.0, total - 2: 5.0})
    runs = cond_mod.persistence(docs, clim)
    assert runs == 2


# --- reasons are plain, and admit what is missing --------------------------

def test_reasons_are_plain_sentences_with_numbers_behind_them(field_conn):
    c, _, _ = _assess(field_conn, days=30)
    assert c.reasons
    for r in c.reasons:
        assert r.plain[0].isupper() and r.plain.endswith(".")
        assert not re.search(r"z-score|MAD|percentile of the residual|n_eff",
                             r.plain)
        assert r.quantity


def test_participant_turnover_is_declared_missing_not_invented(field_conn):
    """The brief asked for "participant turnover is elevated". It cannot be
    measured, so it is named as absent rather than approximated."""
    c, _, _ = _assess(field_conn, days=30)
    joined = " ".join(c.cannot_see).lower()
    assert "how many people" in joined
    assert "by construction" in joined
    for r in c.reasons:
        assert "participant" not in r.plain.lower()
        assert "newcomer" not in r.plain.lower()


def test_conditions_dict_has_no_actor_shaped_field(field_conn):
    c, _, _ = _assess(field_conn, days=30)
    d = c.as_dict()
    for banned in ("actor", "account", "user", "who", "blame", "responsible",
                   "reputation", "score"):
        assert banned not in d


# --- the rendered public tier ----------------------------------------------

@pytest.fixture()
def page(field_conn):
    c, docs, clim = _assess(field_conn, days=30)
    cond = c.as_dict()
    cond["criteria_table"] = [
        (cond_mod.STATE_LABEL[s], t) for s, t in cond_mod.CRITERIA]
    return viz.render_page(docs, clim, {"generated_at": "t"}, cond), c


def test_first_screen_states_conditions_before_any_machinery(page):
    html, c = page
    where_state = html.index(c.headline)
    where_tech = html.index("Technical detail")
    assert where_state < where_tech, "the state must come first"
    assert where_state < html.index("n_eff")


def test_technical_detail_is_behind_disclosure(page):
    html, _ = page
    assert "<details><summary>Technical detail" in html
    assert "<details open><summary>Why these conditions?" in html


def test_public_tier_carries_no_jargon(page):
    """Everything before the technical disclosure must be readable cold."""
    html, _ = page
    public = html[:html.index("Technical detail")].lower()
    for jargon in ("z-score", "mad", "n_eff", "autocorrel", "herfindahl",
                   "percentile of", "coefficient of variation", "ar(1)"):
        assert jargon not in public, f"public tier says {jargon!r}"


def test_criteria_table_is_rendered_for_checking(page):
    html, _ = page
    for _, text in cond_mod.CRITERIA:
        assert text[:40] in html
    assert "published so the label can be checked" in html


def test_page_shows_what_it_cannot_see_next_to_the_state(page):
    html, _ = page
    public = html[:html.index("Technical detail")]
    assert "cannot" in public.lower()
    assert "how many people are involved" in public.lower()


def test_radar_axes_are_hour_and_ratio_not_space(page):
    html, _ = page
    assert "hour of day" in html.lower()
    for bad in ("latitude", "longitude", "globe", "world"):
        assert bad not in html.lower()


def test_conditions_use_the_latest_complete_window(field_conn):
    """The window in flight is partial by definition. Keying on it made the
    live page read "unavailable" almost permanently."""
    total = 8 * 24
    write_hourly_run(field_conn, "run-a", n_days=8)
    runs = query.compatible_runs(field_conn, ENDPOINT)
    _, clim, obs, _ = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    docs = [o.as_dict() for o in obs]

    # Append a partial, ineligible window of the kind the collector always has
    # in flight, and confirm the state still comes from the last full one.
    tail = dict(docs[-1])
    tail["confidence"] = dict(tail["confidence"])
    tail["confidence"]["eligible"] = False
    tail["confidence"]["coverage"] = 0.2
    tail["ts_start"], tail["ts_end"] = "2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z"
    docs.append(tail)

    c = cond_mod.assess(docs, clim.as_dict())
    assert c.state != cond_mod.UNAVAILABLE
    assert c.as_of == obs[-1].ts_end, "reports the last complete reading"
    assert "not yet complete" in c.confidence_plain


def test_all_windows_ineligible_is_still_unavailable(field_conn):
    write_hourly_run(field_conn, "run-a", n_days=8)
    runs = query.compatible_runs(field_conn, ENDPOINT)
    _, clim, obs, _ = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    docs = []
    for o in obs:
        d = o.as_dict()
        d["confidence"] = dict(d["confidence"])
        d["confidence"]["eligible"] = False
        docs.append(d)
    c = cond_mod.assess(docs, clim.as_dict())
    assert c.state == cond_mod.UNAVAILABLE
    assert "cleanly enough" in c.plain


def test_negative_candidate_excess_is_explained_not_left_to_read_as_calm():
    from weatherwatch.social.field.climatology import candidate_summary
    note = candidate_summary([], _EmptyClim(), [])["note"]
    assert "BELOW 10%" in note
    assert "negative excess is therefore expected" in note
    assert "fitted to the same windows" in note


class _EmptyClim:
    quantities: dict = {}
