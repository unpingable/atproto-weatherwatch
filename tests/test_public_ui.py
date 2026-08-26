"""The public page as a designed artifact: what a stranger meets, in what order.

These tests are about presentation, and they exist because presentation is
where this instrument is most likely to lie. Not by printing a wrong number —
the measurement lane has its own tests — but by putting a reassuring word where
a missing measurement should be, by letting a refusal fall below the fold or
behind a click, or by letting a chart grow without a ceiling until nobody on a
phone can load the page at all.

Nothing here asserts a threshold, a rule, or a figure. Those belong to
`conditions.py` and are tested against it. What is asserted here is *order*,
*adjacency*, *degradation* and *bounds*.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

import pytest

from weatherwatch import db, report
from weatherwatch.social import store as social_store
from weatherwatch.social.field import conditions as cond_mod
from weatherwatch.social.field import hero
from weatherwatch.social.field import observation as fobs
from weatherwatch.social.field import run as frun
from tests.conftest import SYNTH_BASE, build_run

FULL = {"post.create": 120, "post.create.reply": 60, "post.create.quote": 10,
        "repost.create": 40, "like.create": 700, "follow.create": 30,
        "block.create": 4, "post.delete": 12, "like.delete": 9,
        "follow.delete": 5, "profile.update": 2, "account.event": 1,
        "identity.event": 1}

HOUR = 3600
FBASE = 1_700_000_000 - (1_700_000_000 % HOUR)
FENDPOINT = "wss://relay-ui.invalid/subscribe"


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", html)


@pytest.fixture()
def plain_db(conn):
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(8)])
    return conn


def _field_store(tmp_path, days=30, spike=None, name="social.sqlite",
                 start_day=0):
    """A sealed field archive, built the way the CLI builds one.

    `start_day` shifts the archive backwards, which is how a test asks for an
    instrument that stopped filing a while ago.
    """
    from tests.social.field.conftest import write_hourly_run, ENDPOINT
    from weatherwatch import query

    agg = db.connect(tmp_path / f"agg-{name}")
    db.init_db(agg)
    write_hourly_run(agg, "run-ui", n_days=days, spike=spike or {},
                     start_day=start_day)
    runs = query.compatible_runs(agg, ENDPOINT)
    _pts, clim, obs, _c = frun.build_all(agg, runs, endpoint=ENDPOINT)
    agg.close()

    path = tmp_path / name
    econn = social_store.connect(path)
    social_store.init_db(econn)
    fobs.init(econn)
    fobs.save_climatology(econn, clim, "2026-01-01T00:00:00Z")
    fobs.save_observations(econn, obs, "2026-01-01T00:00:00Z")
    econn.commit()
    econn.close()
    return path, clim, obs


# --- 1. the reading comes before the paperwork -----------------------------

def test_the_state_precedes_the_receipts(plain_db, tmp_path):
    """Anchored on markup, not on prose.

    This matched the literal "The receipts", which also occurs in a stylesheet
    comment explaining the receipts summaries' touch-target size — so the
    assertion silently started comparing against a byte offset inside the
    `<style>` block. The page keeps its implementation comments deliberately;
    the test is what had to become specific.
    """
    out = tmp_path / "site"
    report.generate_report(plain_db, out)
    html = report_html = (out / "index.html").read_text()
    body = html[html.index("<body"):]
    state = body.index("Station offline")
    assert state < body.index('class="deck"')
    assert state < body.index("A &middot; Observation status") if \
        "A &middot;" in report_html else state < body.index("A · Observation status")


def test_the_scope_denial_still_comes_first(plain_db, tmp_path):
    """The reading may lead, but not past the correction.

    The name misleads, which is the whole reason the denial exists; a visitor
    who reads four words and leaves must still have met it.
    """
    out = tmp_path / "site"
    report.generate_report(plain_db, out)
    html = (out / "index.html").read_text()
    assert html.index("does not measure") < html.index("Station offline")


# --- 2. the refusal travels with the reading -------------------------------

def test_the_refusal_sits_in_the_same_block_as_the_state(plain_db, tmp_path):
    """A cropped screenshot of the state should carry its principal non-claim.

    Adjacency is the requirement, so the assertion is a distance: the universal
    non-claim must be within a screen's worth of markup of the state word, not
    merely present somewhere on the page.
    """
    out = tmp_path / "site"
    report.generate_report(plain_db, out)
    html = (out / "index.html").read_text()
    state = html.index("Station offline")
    nobs = html.index("Not observed", state)
    assert nobs - state < 1200, "the refusal is not adjacent to the state"
    assert "user intent, emotional state" in html[nobs:nobs + 400]


def test_no_disclosure_element_stands_between_the_state_and_its_refusal(
        plain_db, tmp_path):
    """A refusal a reader can collapse is a refusal a screenshot can omit."""
    out = tmp_path / "site"
    report.generate_report(plain_db, out)
    html = (out / "index.html").read_text()
    state = html.index("Station offline")
    assert "<details" not in html[:html.index("Not observed", state)]


# --- 3. missing measurement never becomes calm -----------------------------

def test_a_report_with_no_field_store_says_the_station_is_offline(
        plain_db, tmp_path):
    out = tmp_path / "site"
    result = report.generate_report(plain_db, out)
    assert result["conditions_state"] == cond_mod.OFFLINE
    html = (out / "index.html").read_text()
    assert "Station offline" in html
    assert "without a field observation store" in html
    # and it must not have reached for the reassuring word
    assert ">Calm<" not in html.split("wx-ladder")[0]


def test_an_unreadable_field_store_says_so_rather_than_raising(
        plain_db, tmp_path):
    """A corrupt store must degrade to offline, not take the page down."""
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"this is not a database")
    out = tmp_path / "site"
    result = report.generate_report(plain_db, out, social_db=broken)
    assert result["conditions_state"] == cond_mod.OFFLINE


def test_a_stale_field_store_is_offline_even_when_ingest_is_current(
        conn, tmp_path):
    """Two lanes, two facts, and the page must not average them.

    The aggregate lane can be perfectly healthy while the field archive has
    stopped being written. Reporting the last sealed reading as current
    conditions is exactly the failure `station_offline` exists to name.
    """
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(8)])
    # The hourly fixture is anchored at the same instant as SYNTH_BASE, so the
    # archive has to be shifted back explicitly: it ends two days before the
    # aggregate run, which is far past STALE_AFTER_WINDOWS.
    path, clim, obs = _field_store(tmp_path, days=30, start_day=-32)
    out = tmp_path / "site"
    # the aggregate lane's clock says "just now"; the archive ended long ago
    now = datetime.datetime.fromtimestamp(SYNTH_BASE + 8 * 60,
                                          tz=datetime.timezone.utc)
    result = report.generate_report(conn, out, now=now, social_db=path)
    html = (out / "index.html").read_text()
    assert result["conditions_state"] == cond_mod.OFFLINE
    assert 'data-freshness="current"' in html, (
        "the ingest lane should still report itself current")
    assert "fact about the instrument" in html


# --- 4. freshness is unmissable --------------------------------------------

def test_the_station_bar_carries_every_freshness_fact(plain_db, tmp_path):
    out = tmp_path / "site"
    report.generate_report(plain_db, out)
    html = (out / "index.html").read_text()
    bar = html[html.index('class="station'):html.index("Observed from")]
    for fact in ("newest complete observation", "this page was published",
                 "observation window"):
        assert fact in bar, f"the station bar omits {fact!r}"
    assert "never a live gauge" in bar
    assert html.index('class="station') < html.index("Station offline")


@pytest.mark.parametrize("state", ["current", "partial", "stale", "unavailable"])
def test_every_freshness_state_has_a_short_meaning(state):
    assert state in report.FRESHNESS_SHORT
    assert report.FRESHNESS_SHORT[state]


def test_unavailable_is_not_described_as_calm():
    assert "not calm" in report.FRESHNESS_SHORT["unavailable"]


# --- 5. charts are bounded --------------------------------------------------

def _windows(conn, n):
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(n)])
    return conn


def test_page_size_does_not_scale_with_the_length_of_the_archive(
        conn, tmp_path):
    """C4, as a test.

    The live page reached 11.5 MB and 70,425 `<rect>` elements because every
    window drew its own marks. Above the budget, ten times the windows must
    cost essentially nothing extra — the chart's size is a property of the
    chart, not of how long the instrument has been running.
    """
    _windows(conn, 2000)
    small = tmp_path / "small"
    report.generate_report(conn, small)
    small_marks = (small / "index.html").read_text().count("<rect")

    big_conn = db.connect(tmp_path / "big.sqlite")
    db.init_db(big_conn)
    _windows(big_conn, 20000)
    big = tmp_path / "big"
    report.generate_report(big_conn, big)
    big_marks = (big / "index.html").read_text().count("<rect")

    assert big_marks < small_marks * 1.5, (
        f"marks grew from {small_marks} to {big_marks} on 10x the windows")
    # and the absolute ceiling holds: one strip plus sixteen sparklines
    assert big_marks < 16 * report.SPARK_COLUMNS + report.STRIP_COLUMNS
    assert big_marks < 20000 / 10, "marks are still tracking window count"


def test_the_health_strip_never_exceeds_its_column_budget(conn, tmp_path):
    _windows(conn, 6000)
    out = tmp_path / "site"
    report.generate_report(conn, out)
    html = (out / "index.html").read_text()
    strip = html[html.index('class="strip"'):]
    strip = strip[:strip.index("</svg>")]
    assert strip.count("<rect") <= report.STRIP_COLUMNS


def test_the_collapse_is_disclosed_on_the_page(conn, tmp_path):
    _windows(conn, 6000)
    out = tmp_path / "site"
    report.generate_report(conn, out)
    html = _text((out / "index.html").read_text())
    assert "Each column spans" in html
    assert "worst</strong> quality among them" in html


def test_the_collapse_is_deterministic(conn, tmp_path):
    _windows(conn, 3000)
    now = datetime.datetime.fromtimestamp(SYNTH_BASE + 3000 * 60,
                                          tz=datetime.timezone.utc)
    a, b = tmp_path / "a", tmp_path / "b"
    report.generate_report(conn, a, now=now)
    report.generate_report(conn, b, now=now)
    assert (a / "index.html").read_text() == (b / "index.html").read_text()


# --- 6. what the collapse must never lose ----------------------------------

def test_one_unobserved_window_survives_a_collapsed_column(conn, tmp_path):
    """Unobserved time must never be outvoted by its clean neighbours.

    This is the whole promise of the strip. A column that averaged, or took
    the commonest quality, would erase a single-window outage entirely — and
    an erased outage looks exactly like quiet.
    """
    # Enough windows that the strip actually collapses, so the lone outage is
    # sharing a column with clean neighbours -- which is the case that matters.
    spec = [{"metrics": dict(FULL)} for _ in range(3300)]
    spec[1500] = None                              # nobody was watching
    build_run(conn, "r1", spec)
    out = tmp_path / "site"
    report.generate_report(conn, out)
    html = (out / "index.html").read_text()
    strip = html[html.index('class="strip"'):]
    strip = strip[:strip.index("</svg>")]
    assert "worst of" in strip, "fixture did not collapse; test proves nothing"
    assert "url(#unobs)" in strip, "a lone unobserved window vanished"


def test_a_spike_inside_a_collapsed_column_survives(conn, tmp_path):
    """The line follows the column maximum, so a one-window burst still shows."""
    spec = [{"metrics": dict(FULL)} for _ in range(900)]
    spec[450] = {"metrics": dict(FULL, **{"post.create": 12000})}
    build_run(conn, "r1", spec)
    from weatherwatch import query
    series = query.series(conn, ["r1"], "post.create")
    cols, per = report._collapse(series.points, report.SPARK_COLUMNS)
    assert per > 1, "fixture must actually collapse"
    assert max(c.hi for c in cols if c.hi is not None) >= 200.0, (
        "the spike was averaged away")


def test_a_collapsed_column_keeps_both_bounds(conn, tmp_path):
    spec = [{"metrics": dict(FULL)} for _ in range(900)]
    spec[450] = {"metrics": dict(FULL, **{"post.create": 12000})}
    build_run(conn, "r1", spec)
    from weatherwatch import query
    cols, per = report._collapse(
        query.series(conn, ["r1"], "post.create").points, report.SPARK_COLUMNS)
    spiked = [c for c in cols if c.hi and c.hi > 100]
    assert spiked and spiked[0].lo < spiked[0].hi, (
        "min and max collapsed to one value; the range is not being drawn")


def test_below_the_budget_nothing_is_collapsed(conn):
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(20)])
    from weatherwatch import query
    cols, per = report._collapse(
        query.series(conn, ["r1"], "post.create").points, report.SPARK_COLUMNS)
    assert per == 1
    assert len(cols) == 20


def test_worst_quality_wins_every_pairing():
    for worse, better in (("unobserved", "clean"), ("gap", "partial"),
                          ("loss", "lagged"), ("degraded", "seam"),
                          ("partial", "warming_up")):
        assert report._worst([better, worse, better]) == worse


# --- 7. the history strip is history, not interpolation --------------------

def test_the_history_strip_reports_a_gap_rather_than_carrying_a_reading(
        tmp_path):
    """No reading is carried forward under a fresher label."""
    _path, clim, obs = _field_store(tmp_path, days=30)
    docs = [o.as_dict() for o in obs]
    # amputate the middle of the archive: 6h ago has nothing to report
    keep = [d for d in docs if not (5 <= (len(docs) - docs.index(d)) <= 14)]
    entries = hero.recent_states(keep, clim.as_dict())
    labels = {e["label"]: e for e in entries}
    assert labels["now"]["state"] is not None
    assert labels["6h ago"]["state"] is None, (
        "a reading was carried across a gap")


def test_the_history_strip_uses_the_same_rule_as_the_headline(tmp_path):
    _path, clim, obs = _field_store(
        tmp_path, days=30, spike={30 * 24 - 1 - i: 4.5 for i in range(4)})
    docs = [o.as_dict() for o in obs]
    cdoc = clim.as_dict()
    headline = cond_mod.assess(docs, cdoc)
    entries = hero.recent_states(docs, cdoc)
    assert entries[0]["label"] == "now"
    assert entries[0]["state"] == headline.state, (
        "the strip and the headline disagree about the present")


def test_an_empty_archive_produces_no_history_rather_than_a_calm_one():
    assert hero.recent_states([], {}) == []


# --- 8. the ladder ---------------------------------------------------------

def test_the_ladder_shows_every_measurable_state_and_marks_the_one_in_force():
    html = hero._ladder(cond_mod.TURBULENT)
    for state in hero.LADDER:
        assert cond_mod.STATE_LABEL[state] in html
    assert html.count(" on\"") == 1 or 'on"' in html
    assert 'aria-current="true"' in html


def test_the_ladder_carries_no_number():
    """A ladder that prints '4 of 6' is a composite severity index.

    This estate has refused those, and the refusal is on the page: the Global
    Beef Index is a joke name for a composite that does not exist.
    """
    html = hero._ladder(cond_mod.STORM)
    assert not re.search(r"\b\d+\s*(of|/)\s*\d+\b", html)


def test_the_two_null_states_are_not_on_the_ladder():
    assert cond_mod.UNAVAILABLE not in hero.LADDER
    assert cond_mod.OFFLINE not in hero.LADDER
    # the note names them when a measurable state is in force ...
    assert "not on this ladder" in hero._ladder(cond_mod.CALM)
    # ... and when neither is, it says no rung applies rather than marking one
    html = hero._ladder(cond_mod.OFFLINE)
    assert "No state on this ladder is in force" in html
    assert 'aria-current="true"' not in html


def test_every_state_has_a_colour_rule():
    """`unsettled` and `station_offline` had none and fell back to body ink."""
    for state in cond_mod.STATE_LABEL:
        assert f".st-{state}{{" in hero.STYLE.replace(" ", "")


# --- 9. machine readers get the reading and its limits ---------------------

def test_summary_json_carries_the_conditions(plain_db, tmp_path):
    out = tmp_path / "site"
    report.generate_report(plain_db, out)
    summary = json.loads((out / "summary.json").read_text())
    assert summary["conditions"]["state"] == cond_mod.OFFLINE
    assert summary["conditions"]["universal_not_observed"]
    assert summary["conditions"]["cannot_see"]


def test_a_machine_reader_cannot_get_the_state_without_its_refusals(
        conn, tmp_path):
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(8)])
    path, _clim, _obs = _field_store(tmp_path, days=30)
    out = tmp_path / "site"
    report.generate_report(conn, out, social_db=path)
    payload = json.loads((out / "summary.json").read_text())["conditions"]
    assert payload["state"]
    for key in ("universal_not_observed", "cannot_see", "criteria"):
        assert payload[key], f"{key} missing from the machine-readable state"


# --- 10. the radar is not a map --------------------------------------------

def test_the_radar_makes_no_geographic_claim(tmp_path):
    _path, clim, obs = _field_store(tmp_path, days=30)
    svg = hero.radar([o.as_dict() for o in obs], clim.as_dict())
    assert svg
    low = svg.lower()
    for token in ("globe", "map of", "country", "latitude", "longitude",
                  "region", "north", "south", "east", "west"):
        assert token not in low, f"the radar says {token!r}"
    assert "not a map" in hero.render(
        {"state": "calm"}, [o.as_dict() for o in obs], clim.as_dict()).lower()


def test_the_radar_carries_no_pixel_size(tmp_path):
    _path, clim, obs = _field_store(tmp_path, days=30)
    svg = hero.radar([o.as_dict() for o in obs], clim.as_dict())
    assert "viewBox=" in svg
    assert not re.search(r'<svg[^>]*\swidth="\d', svg)
    assert not re.search(r'<svg[^>]*\sheight="\d', svg)


def test_the_radar_scale_is_symmetric_about_typical():
    """"Typical at mid-radius" has to be true, not just intended."""
    import math
    assert math.isclose(hero.RADAR_LO * hero.RADAR_HI, 1.0)


def test_a_missing_baseline_draws_no_radar_at_all():
    assert hero.radar([], {}) == ""


# --- 11. the interpreter this renders on is not the one it deploys on ------

def test_the_ladder_markup_parses_on_the_python_that_serves_it():
    """The serving host is 3.10; this workstation is 3.12.

    PEP 701 lifted the f-string restrictions in 3.12, so an escaped quote
    inside an f-string expression renders perfectly here and is a SyntaxError
    where the page is actually built. This is the second time the gap between
    the development interpreter and the deployed one has produced a red build
    in this repository.
    """
    import sys as _sys
    sys_path = pathlib.Path(hero.__file__).resolve().parents[4] / "spike"
    _sys.path.insert(0, str(sys_path))
    try:
        import check_py310_fstrings as guard
    finally:
        _sys.path.remove(str(sys_path))
    assert guard.offences(pathlib.Path(hero.__file__)) == []


def test_the_whole_tree_is_free_of_post_311_fstring_syntax():
    import subprocess
    root = pathlib.Path(hero.__file__).resolve().parents[4]
    result = subprocess.run(
        [__import__("sys").executable, "spike/check_py310_fstrings.py"],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


BAD_BACKSLASH = 'x = 1\ny = f"a{\' q=\\"v\\"\' if x else \'\'}b"\n'
BAD_REUSED = 'd = {}\ny = f"{d["k"]}"\n'
GOOD_NESTED = 'd = {"k": 1}\ny = f"""<p>{d["k"]}</p>"""\n'


def test_the_guard_actually_catches_the_bug_it_was_written_for(tmp_path):
    """A guard that passes everything is not a guard.

    On 3.11 and earlier there is no `FSTRING_START` token and the guard is a
    no-op by construction -- because there the *interpreter* is the guard, and
    `compileall` has already rejected the file. So the assertion flips with
    the tokenizer: the checker catches these on 3.12+, and `compile()` refuses
    them everywhere else. Either way nothing gets through; only the thing
    doing the refusing changes.
    """
    import sys as _sys
    sys_path = pathlib.Path(hero.__file__).resolve().parents[4] / "spike"
    _sys.path.insert(0, str(sys_path))
    try:
        import check_py310_fstrings as guard
    finally:
        _sys.path.remove(str(sys_path))

    modern = hasattr(__import__("token"), "FSTRING_START")
    for name, source in (("backslash", BAD_BACKSLASH), ("reused", BAD_REUSED)):
        path = tmp_path / f"{name}.py"
        path.write_text(source)
        if modern:
            assert guard.offences(path), f"{name}: 3.12 tokenizer missed it"
        else:
            with pytest.raises(SyntaxError):
                compile(source, str(path), "exec")

    # The legal form this codebase relies on everywhere must stay quiet, and
    # must actually compile, on every version.
    fine = tmp_path / "fine.py"
    fine.write_text(GOOD_NESTED)
    compile(GOOD_NESTED, str(fine), "exec")
    assert guard.offences(fine) == [], "triple-quoted f-string flagged"
