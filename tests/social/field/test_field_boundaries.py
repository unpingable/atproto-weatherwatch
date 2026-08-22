"""Qualification: the instrument cannot answer the questions it must not.

Each test here corresponds to one of the campaign's stated guarantees. They
are written to fail loudly rather than to document intent.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re

import pytest

from weatherwatch import query
from weatherwatch.classify import ALLOWED_METRICS
from weatherwatch.social.field import climatology as clim_mod
from weatherwatch.social.field import observation as obs_mod
from weatherwatch.social.field import quantities as q_mod
from weatherwatch.social.field import run, viz
from weatherwatch.social.field.quantities import FIELD_METRICS, QUANTITIES

from .conftest import ENDPOINT, write_hourly_run

FIELD_PKG = pathlib.Path(q_mod.__file__).resolve().parent

#: The same expression `deploy/publish.sh` greps a build directory with,
#: plus salted actor tokens.
IDENTITY_RE = re.compile(
    r"did:(plc|web|key):|at://|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)|\ba:[0-9a-f]{12}\b")


@pytest.fixture()
def built(field_conn):
    write_hourly_run(field_conn, "run-a", n_days=30)
    runs = query.compatible_runs(field_conn, ENDPOINT)
    return run.build_all(field_conn, runs, endpoint=ENDPOINT)


@pytest.fixture()
def page(built):
    points, clim, obs, cands = built
    docs = [o.as_dict() for o in obs]
    return viz.render_page(docs, clim.as_dict(), {"generated_at": "t"})


@pytest.fixture()
def page_text(page):
    """Page with whitespace collapsed.

    Assertions about what the page *says* must not depend on where its source
    happens to wrap a line.
    """
    return re.sub(r"\s+", " ", page).lower()


# --- 1. no identity leakage -------------------------------------------------

def test_field_reads_only_identity_free_counters():
    """Upstream guarantee: every metric the field vector touches is a member
    of `classify`'s finite alphabet, which cannot contain a DID."""
    assert set(FIELD_METRICS) <= set(ALLOWED_METRICS)


def test_no_observation_carries_an_identifier(built):
    _, _, obs, _ = built
    blob = json.dumps([o.as_dict() for o in obs], sort_keys=True)
    assert not IDENTITY_RE.search(blob)


def test_climatology_carries_no_identifier(built):
    _, clim, _, _ = built
    assert not IDENTITY_RE.search(json.dumps(clim.as_dict(), sort_keys=True))


def test_rendered_page_carries_no_identifier(page):
    assert not IDENTITY_RE.search(page)


def test_no_per_actor_quantity_exists():
    """There is no participant count, and it is not an oversight."""
    for q in QUANTITIES:
        low = q.name.lower()
        for banned in ("actor", "user", "account", "participant", "author",
                       "handle", "did"):
            assert banned not in low, f"{q.name} is actor-shaped"
    assert "participants" in obs_mod.STRUCTURAL_ABSENCES
    assert "construction" in obs_mod.STRUCTURAL_ABSENCES["participants"]


# --- 2. no geographic inference --------------------------------------------

GEO_TOKENS = ("latitude", "longitude", "geoip", "geolocat", "country_code",
              "lat_lon", "coordinates", "timezone_of", "region_of",
              "ip_address", "maxmind", "globe")


def test_no_module_reaches_for_geography():
    for path in sorted(FIELD_PKG.rglob("*.py")):
        text = path.read_text().lower()
        for token in GEO_TOKENS:
            if token == "globe":
                continue          # discussed in prose; never computed
            assert token not in text, f"{path.name} mentions {token!r}"


def test_no_quantity_is_spatial():
    for q in QUANTITIES:
        assert "geo" not in q.name and "location" not in q.name
        assert "region" not in q.name


def test_absence_of_geography_is_documented_as_a_property(page_text):
    assert "location" in obs_mod.STRUCTURAL_ABSENCES
    assert "ATProto exposes none" in obs_mod.STRUCTURAL_ABSENCES["location"]
    assert "no geography" in page_text


def test_page_never_implies_a_place(page_text):
    low = page_text
    for token in ("globe", "world map", "continent", "country", "latitude",
                  "longitude", "region of the world"):
        assert token not in low, f"page implies place via {token!r}"


# --- 3. no causal or accusatory language -----------------------------------

CAUSAL = ("caused by", "because of", "responsible for", "to blame",
          "culprit", "guilty", "perpetrator", "instigator")
ACCUSATORY = ("toxic", "harass", "brigad", "abusive", "bad actor",
              "offender", "malicious", "hostile actor")


def test_no_quantity_name_or_summary_makes_a_causal_claim():
    """Non-claims deliberately *mention* words like hostility in order to deny
    them, so only the affirmative text is scanned."""
    for q in QUANTITIES:
        text = (q.name + " " + q.summary).lower()
        for bad in CAUSAL + ACCUSATORY:
            assert bad not in text, f"{q.name} summary says {bad!r}"


def test_every_quantity_states_what_it_does_not_measure():
    for q in QUANTITIES:
        assert q.non_claim and len(q.non_claim) > 20
        assert q.non_claim.lower().startswith(("not ", "counts ", "no "))


def test_observations_carry_assembled_non_claims(built):
    _, _, obs, _ = built
    o = next(o for o in obs if o.metrics)
    joined = " ".join(o.non_claims).lower()
    assert "not causation" in joined
    for name in o.metrics:
        assert any(nc.startswith(f"{name}:") for nc in o.non_claims)


def test_page_states_the_non_causal_posture(page_text):
    assert "observation is not causation" in page_text
    assert "cannot report who caused it" in page_text
    assert "climatology, not an alarm system" in page_text
    for bad in CAUSAL:
        assert bad not in page_text, f"page says {bad!r}"


def test_candidates_are_typed_as_candidates_not_findings(built):
    _, _, _, cands = built
    assert cands
    for c in cands[:20]:
        assert "not a finding" in c.note
        assert "attributable" in c.note
    fields = set(clim_mod.Candidate.__dataclass_fields__)
    for banned in ("severity", "score", "actor", "who", "blame", "risk"):
        assert banned not in fields


# --- 4. stable replay from stored observations -----------------------------

def test_observation_id_replays_from_its_own_document(built):
    _, _, obs, _ = built
    for o in obs[:200]:
        assert run.replay_observation(o.as_dict()) == o.observation_id


def test_ids_are_stable_across_rebuilds(field_conn):
    write_hourly_run(field_conn, "run-a", n_days=8)
    runs = query.compatible_runs(field_conn, ENDPOINT)
    a = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    b = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    assert a[1].climatology_id == b[1].climatology_id
    assert ([o.observation_id for o in a[2]]
            == [o.observation_id for o in b[2]])


def test_round_trip_through_storage_preserves_identity(built, tmp_path):
    from weatherwatch.social import store
    _, clim, obs, _ = built
    conn = store.connect(tmp_path / "s.sqlite")
    store.init_db(conn)
    obs_mod.init(conn)
    obs_mod.save_climatology(conn, clim, "t")
    obs_mod.save_observations(conn, obs, "t")

    docs, total = obs_mod.load_observations(conn)
    assert len(docs) == len(obs) == total
    for d in docs:
        assert run.replay_observation(d) == d["observation_id"]
    loaded_clim = obs_mod.load_climatology(conn)
    assert loaded_clim["climatology_id"] == clim.climatology_id
    conn.close()


def test_page_renders_identically_from_storage_twice(built, tmp_path):
    from weatherwatch.social import store
    _, clim, obs, _ = built
    conn = store.connect(tmp_path / "s.sqlite")
    store.init_db(conn)
    obs_mod.init(conn)
    obs_mod.save_climatology(conn, clim, "t")
    obs_mod.save_observations(conn, obs, "t")
    cdoc = obs_mod.load_climatology(conn)
    meta = {"generated_at": "fixed"}
    a = viz.render_page(obs_mod.load_observations(conn)[0], cdoc, meta)
    b = viz.render_page(obs_mod.load_observations(conn)[0], cdoc, meta)
    assert a == b
    conn.close()


# --- 5. measuring activity, not judging participants ------------------------

def test_observation_has_no_verdict_shaped_field():
    fields = set(obs_mod.SocialWeatherObservation.__dataclass_fields__)
    for banned in ("severity", "verdict", "risk", "score", "rating",
                   "reputation", "trust", "actor", "subject"):
        assert banned not in fields
    assert {"metrics", "confidence", "provenance",
            "non_claims", "unavailable"} <= fields


def test_confidence_can_say_it_knows_little(field_conn):
    write_hourly_run(field_conn, "run-a", n_days=3)
    runs = query.compatible_runs(field_conn, ENDPOINT)
    _, clim, obs, _ = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    assert all(o.confidence.support == clim_mod.UNSUPPORTED for o in obs)
    assert any("gap is visible" in o.confidence.note for o in obs)


def test_unmeasurable_quantities_are_recorded_with_a_reason(field_conn):
    write_hourly_run(field_conn, "run-a", n_days=8, unobserved={10, 11})
    runs = query.compatible_runs(field_conn, ENDPOINT)
    _, _, obs, _ = run.build_all(field_conn, runs, endpoint=ENDPOINT)
    first = obs[0]
    assert first.unavailable, "the first window has no trailing context"
    assert all(isinstance(v, str) and v for v in first.unavailable.values())
    assert set(first.unavailable) & {"turbulence", "acceleration"}


def test_page_offers_no_lookup_ranking_or_action(page_text):
    low = page_text
    for shape in ("<input", "<form", "<select", "<button", "onclick=",
                  "leaderboard", "report this", "take action", "moderat"):
        assert shape not in low


def test_selection_of_quantities_is_fixed_not_configurable():
    """A tunable quantity set would let a caller choose what 'the weather' is."""
    sig = inspect.signature(q_mod.build_field).parameters
    assert list(sig) == ["series_map"]


def test_loader_returns_the_newest_observations_not_the_oldest(built, tmp_path):
    """Ascending ORDER BY plus LIMIT takes the OLDEST rows. The live page
    reported conditions a week stale for exactly this reason."""
    from weatherwatch.social import store

    _, clim, obs, _ = built
    conn = store.connect(tmp_path / "s.sqlite")
    store.init_db(conn)
    obs_mod.init(conn)
    obs_mod.save_climatology(conn, clim, "t")
    obs_mod.save_observations(conn, obs, "t")

    docs, total = obs_mod.load_observations(conn, limit=10)
    assert total == len(obs), "the total is the archive, not the page"
    assert len(docs) == 10
    newest = sorted(o.ts_end for o in obs)[-1]
    assert docs[-1]["ts_end"] == newest, "must end at the most recent window"
    assert docs[0]["ts_start"] < docs[-1]["ts_start"], "oldest-first ordering"
    conn.close()


def test_page_discloses_when_the_archive_is_larger_than_the_page(built):
    _, clim, obs, _ = built
    docs = [o.as_dict() for o in obs][:50]
    html = viz.render_page(docs, clim.as_dict(),
                           {"generated_at": "t",
                            "observations_in_store": 20000})
    assert "most recent of 20,000 in the archive" in html
    assert "older not drawn" in html


def test_observations_load_scoped_to_their_own_climatology(built, tmp_path):
    """Readings scored against a four-day baseline are not comparable with
    readings scored against a fortnight's, so a page shows one baseline."""
    from weatherwatch.social import store

    _, clim, obs, _ = built
    conn = store.connect(tmp_path / "s.sqlite")
    store.init_db(conn)
    obs_mod.init(conn)
    obs_mod.save_climatology(conn, clim, "t")
    obs_mod.save_observations(conn, obs, "t")

    # A second run over the same windows, scored against a different baseline.
    other = [o.__class__(**{**o.__dict__,
                            "provenance": {**o.provenance,
                                           "climatology_id": "deadbeef"}})
             for o in obs[:5]]
    obs_mod.save_observations(conn, other, "t")

    scoped, total = obs_mod.load_observations(
        conn, climatology_id=clim.climatology_id)
    assert all(d["provenance"]["climatology_id"] == clim.climatology_id
               for d in scoped)
    assert total == len(scoped)
    everything, _ = obs_mod.load_observations(conn)
    assert len(everything) > len(scoped)
    conn.close()
