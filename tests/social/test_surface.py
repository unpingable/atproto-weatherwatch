"""The published surface: projection, JSON artifact, and rendered section.

The question every test here asks is the same one `deploy/publish.sh` asks
before bytes leave the machine: can a reader of this artifact learn anything
about a particular account?
"""

from __future__ import annotations

import json
import re

import pytest

from weatherwatch.social import api, episodes, projection, section, store
from weatherwatch.social.edges import StatusEvent
from weatherwatch.social.sensors import aggregate

from .conftest import BASE_US, build_run, edge as mk, steady_then_burst
from .conftest import write_edges, write_status

RUN = "run-surface"
SEC = 1_000_000

#: The exact expression `deploy/publish.sh` greps the build directory with.
PUBLISH_GATE = re.compile(
    r"did:(plc|web|key):|at://|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)")


def _mixed_store(conn, edge_conn):
    """Aggregate episodes plus edge and lifecycle ones in the same store."""
    windows = []
    for i in range(20):
        windows.append({"metrics": {"block.create": 10 + (i % 3) - 1,
                                    "like.create": 10 + (i % 3) - 1}})
    for _ in range(4):
        windows.append({"metrics": {"block.create": 90, "like.create": 90}})
    windows += [{"metrics": {"block.create": 10, "like.create": 10}}] * 6
    build_run(conn, RUN, windows)
    sealed = episodes.run_aggregate(
        conn, [RUN], ["block.create", "like.create"], None, None,
        aggregate.AggregateConfig())

    targets = [f"did:plc:shared{i:02d}" for i in range(10)]
    write_edges(edge_conn, [mk(f"did:plc:cohort{a}", t, BASE_US + (a * 5 + i) * SEC)
                            for a in range(8) for i, t in enumerate(targets)])
    write_status(edge_conn, [StatusEvent(BASE_US + 100 * SEC,
                                         "did:plc:shared00", 0, "deactivated")])
    sealed += episodes.run_edge(edge_conn, ["block"], BASE_US,
                                BASE_US + 3600 * SEC)
    sealed += episodes.run_lifecycle(edge_conn, BASE_US, BASE_US + 3600 * SEC)
    episodes.persist(edge_conn, sealed)
    return sealed


# --- audience gating --------------------------------------------------------

def test_public_projection_admits_only_the_aggregate_detector(conn, edge_conn,
                                                              tmp_path):
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]

    pub = projection.load(path, audience=projection.AUDIENCE_PUBLIC)
    loc = projection.load(path, audience=projection.AUDIENCE_LOCAL)

    assert pub.available and loc.available
    assert {e.detector_id for e in pub.episodes} == {"aggregate_rate_episode"}
    assert len(loc.episodes) > len(pub.episodes), "local sees more"
    assert {"edge_structure_episode", "account_lifecycle_episode"} <= {
        e.detector_id for e in loc.episodes}


def test_public_projection_is_identity_free(conn, edge_conn):
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    pub = projection.load(path, audience=projection.AUDIENCE_PUBLIC)
    blob = json.dumps(pub.as_dict(), sort_keys=True)
    assert not PUBLISH_GATE.search(blob)
    assert not re.search(r"\ba:[0-9a-f]{12}\b", blob)


def test_the_episode_view_has_no_actor_shaped_field():
    """A whitelist, so a detector adding an identity key cannot leak by
    passthrough. This test is the whitelist's guard."""
    fields = set(projection.EpisodeView.__dataclass_fields__)
    for bad in ("actor", "actor_tokens", "target", "target_token", "did",
                "handle", "subject_ref", "cohort", "members"):
        assert bad not in fields
    assert projection.IDENTITY_EXPLAIN_KEYS.isdisjoint(fields)


def test_an_actor_bearing_episode_reaching_public_is_an_error(conn, edge_conn,
                                                              monkeypatch):
    """If PUBLIC_DETECTORS is ever widened by accident, the projection must
    fail loudly rather than publish."""
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(projection, "PUBLIC_DETECTORS",
                        frozenset({"aggregate_rate_episode",
                                   "edge_structure_episode"}))
    with pytest.raises(projection.IdentityLeak):
        projection.load(path, audience=projection.AUDIENCE_PUBLIC)


def test_assert_identity_free_catches_each_identifier_shape():
    for bad in ("did:plc:abc", "did:web:example.com", "at://x/y",
                "bafyreiabcdefghij", "someone.bsky.social", "a:0123456789ab"):
        with pytest.raises(projection.IdentityLeak):
            projection.assert_identity_free({"leak": bad})


# --- the JSON artifact ------------------------------------------------------

def test_api_artifact_is_versioned_and_gated(conn, edge_conn, tmp_path):
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    pub = projection.load(path, audience=projection.AUDIENCE_PUBLIC)
    out = api.write(pub, tmp_path)
    doc = json.loads(out.read_text())

    assert doc["schema"] == api.SCHEMA
    assert doc["source"]["detector_allowlist"] == ["aggregate_rate_episode"]
    assert "not causation" in doc["disclaimer"]
    assert not PUBLISH_GATE.search(out.read_text())


def test_views_are_identity_free_for_every_audience(conn, edge_conn):
    """Stronger than the audience gate, and worth stating separately.

    `EpisodeView` is a whitelist, so actor tokens never reach the view layer
    at all -- not even under `local`. The audience split decides which
    *detectors* a reader sees, not whether identity was scrubbed out of them.
    Salted tokens exist only on the sealed envelopes in storage and in the
    local seismogram, which reads those envelopes directly.
    """
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    loc = projection.load(path, audience=projection.AUDIENCE_LOCAL)
    assert any(e.detector_id == "edge_structure_episode" for e in loc.episodes)
    blob = json.dumps(loc.as_dict(), sort_keys=True)
    assert not PUBLISH_GATE.search(blob)
    assert not re.search(r"\ba:[0-9a-f]{12}\b", blob)

    # ...and the tokens really are in storage, so the absence above is the
    # projection's doing rather than the detector never having produced any.
    raw = edge_conn.execute(
        "SELECT envelope_json FROM episode WHERE detector_id=?",
        ("edge_structure_episode",)).fetchone()[0]
    assert "actor_tokens" in raw


def test_api_refuses_to_write_a_leaking_public_document(tmp_path):
    """The last gate before bytes are written, tested against a payload that
    really does carry an identifier."""
    leaking = projection.SocialProjection(
        audience=projection.AUDIENCE_PUBLIC, available=True, reason="",
        episodes=(), summary={"note": "did:plc:oops"}, source={})
    with pytest.raises(projection.IdentityLeak):
        api.build(leaking)
    assert not (tmp_path / api.ARTIFACT_NAME).exists()


# --- the rendered section ---------------------------------------------------

def _public(conn, edge_conn, receipt=None):
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    return projection.load(path, audience=projection.AUDIENCE_PUBLIC,
                           sink_receipt=receipt)


def test_section_renders_and_passes_the_publish_gate(conn, edge_conn):
    html = section.render(_public(conn, edge_conn))
    assert not PUBLISH_GATE.search(html)
    assert not re.search(r"\ba:[0-9a-f]{12}\b", html)
    assert "<svg" in html and "block_burst" in html


def test_section_states_the_retention_posture_when_off(conn, edge_conn):
    html = section.render(_public(conn, edge_conn, receipt={
        "enabled": False, "collections": [], "retention": None,
        "config_source": "default", "config_hash": "abc"}))
    assert "OFF" in html


def test_section_states_the_retention_posture_when_on(conn, edge_conn):
    html = section.render(_public(conn, edge_conn, receipt={
        "enabled": True, "collections": ["block", "listitem"],
        "retention": "24h", "config_source": "env", "config_hash": "abc"}))
    assert "ON" in html
    assert "block, listitem" in html and "24h" in html


def test_section_renders_when_nothing_is_available():
    """The receipt must appear even with nothing detected; a receipt that only
    shows up when the answer is interesting is not a receipt."""
    empty = projection.SocialProjection(
        audience=projection.AUDIENCE_PUBLIC, available=False,
        reason="no episode store", source={},
        sink_receipt={"enabled": False, "collections": [], "retention": None,
                      "config_source": "default", "config_hash": "abc"})
    html = section.render(empty)
    assert "OFF" in html and "no episode store" in html


def test_section_makes_no_causal_or_accusatory_claim(conn, edge_conn):
    html = section.render(_public(conn, edge_conn)).lower()
    for word in ("brigad", "harass", "coordinat", "attack", "abuse", "mob",
                 "bad actor", "offender", "perpetrator", "responsible for",
                 "caused by", "because of"):
        assert word not in html, f"section says {word!r}"
    assert "not causation" in html
    assert "provisional" in html


def test_section_has_no_lookup_or_ranking_surface(conn, edge_conn):
    html = section.render(_public(conn, edge_conn)).lower()
    for shape in ("<input", "<form", "<select", "<button", "onclick=",
                  "fetch(", "leaderboard", "top blockers"):
        assert shape not in html


def test_negative_cases_stay_visible(conn, edge_conn):
    """Deficits and quiet cases must reach the surface. A view that only ever
    shows surges reads as a catalogue of incidents."""
    windows = [{"metrics": {"block.create": 100 + (i % 5)}} for i in range(20)]
    windows += [{"metrics": {"block.create": 5}} for _ in range(4)]
    windows += [{"metrics": {"block.create": 100}} for _ in range(4)]
    build_run(conn, RUN, windows)
    sealed = episodes.run_aggregate(conn, [RUN], ["block.create"], None, None,
                                    aggregate.AggregateConfig())
    episodes.persist(edge_conn, sealed)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    pub = projection.load(path, audience=projection.AUDIENCE_PUBLIC)

    assert any(e.direction == "deficit" for e in pub.episodes)
    assert "deficit" in pub.summary["by_direction"]
    html = section.render(pub)
    assert "block_lull" in html
    assert "excess / deficit" in html


def test_every_row_carries_the_statistic_behind_its_band(conn, edge_conn):
    """The bands are provisional, so a reader must be able to ignore them."""
    pub = _public(conn, edge_conn)
    for e in pub.episodes:
        assert e.peak_z is not None
        assert e.rate_ratio is not None
        assert e.baseline_estimator
    html = section.render(pub)
    assert "peak z" in html and "ratio" in html


# --- no silent caps ---------------------------------------------------------

def test_table_floor_and_cap_are_disclosed_not_silent(conn, edge_conn):
    """A bounded view must say what it bounded. Silent truncation reads as
    'covered everything' when it did not."""
    # A smooth, high-volume baseline: statistically tiny changes clear any z
    # gate while remaining tiny changes. That is the real shape this floor
    # exists for, observed on the deployed instrument as a 1.03x like storm.
    def calm(n):
        return [{"metrics": {"block.create": 1000 + (i % 3)}} for i in range(n)]

    windows = calm(20)
    for _ in range(3):
        windows += [{"metrics": {"block.create": 1010}} for _ in range(3)]
        windows += calm(8)
    windows += [{"metrics": {"block.create": 9000}} for _ in range(3)]
    windows += calm(5)
    build_run(conn, RUN, windows)
    sealed = episodes.run_aggregate(conn, [RUN], ["block.create"], None, None,
                                    aggregate.AggregateConfig())
    episodes.persist(edge_conn, sealed)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    pub = projection.load(path, audience=projection.AUDIENCE_PUBLIC)

    small = [e for e in pub.episodes if e.magnitude < 1.0]
    assert small, "fixture must contain sub-floor episodes to hold back"

    html = section.render(pub, floor=1.0)
    assert f"of {len(pub.episodes)} episodes in this window" in html
    assert f"{len(small)} fall below magnitude 1" in html
    assert "nothing here is dropped without being counted" in html
    # every held-back episode still reaches the machine-readable side
    doc = api.build(pub)
    assert len(doc["episodes"]) == len(pub.episodes)


def test_section_states_the_window_it_covers(conn, edge_conn):
    pub = _public(conn, edge_conn)
    html = section.render(pub)
    assert pub.summary["first_ts"] in html
    assert pub.summary["last_ts"] in html


def test_report_window_bounds_the_published_projection(conn, edge_conn,
                                                       tmp_path):
    """The page reaches back a stated distance, not 'everything ever'."""
    from weatherwatch.report import _load_social
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]

    wide = _load_social(conn, path, window_s=None)
    narrow = _load_social(conn, path, window_s=60)
    assert wide.available
    assert len(narrow.episodes) < len(wide.episodes)


# --- re-detection ----------------------------------------------------------

def test_re_detection_does_not_inflate_the_projection(conn, edge_conn):
    """Measured on the deployed store: a second pass over a shifted window
    took 1,885 rows to 2,242 while adding 9 real episodes.

    The scope-derived watermark rides in `window_fingerprint`, so the same
    episode observed under a different scope seals under a new `det_id`.
    `episode_id` is derived from the evidence segment alone and stays put, so
    the read model collapses on that and storage keeps every detection.
    """
    from .conftest import SYNTH_BASE, SYNTH_WIDTH

    def calm(n):
        return [{"metrics": {"block.create": 10 + (i % 3) - 1}}
                for i in range(n)]

    # Two unobserved windows near the start, so a scope that includes them and
    # one that does not carry different coverage -- which is what puts a
    # different watermark, and therefore a different det_id, on the same
    # episode. Without that the two passes seal identically and collapse.
    windows = calm(4) + [None, None] + calm(10)
    windows += [{"metrics": {"block.create": 90}} for _ in range(4)]
    windows += calm(6)
    build_run(conn, RUN, windows)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    base_us = SYNTH_BASE * 1_000_000
    span_us = len(windows) * SYNTH_WIDTH * 1_000_000

    first = episodes.run_aggregate(conn, [RUN], ["block.create"],
                                   base_us, base_us + span_us,
                                   aggregate.AggregateConfig())
    episodes.persist(edge_conn, first)
    once = projection.load(path, audience=projection.AUDIENCE_PUBLIC)
    assert once.episodes, "fixture must produce an episode"

    # Same episode, scope starting after the hole: coverage differs.
    again = episodes.run_aggregate(
        conn, [RUN], ["block.create"],
        base_us + 6 * SYNTH_WIDTH * 1_000_000,
        base_us + span_us, aggregate.AggregateConfig())
    episodes.persist(edge_conn, again)
    twice = projection.load(path, audience=projection.AUDIENCE_PUBLIC)

    stored = edge_conn.execute("SELECT COUNT(*) FROM episode").fetchone()[0]
    assert stored > len(twice.episodes), "storage keeps every detection"
    assert len(twice.episodes) == len(once.episodes), "the page does not grow"
    assert twice.summary["n_superseded"] == stored - len(twice.episodes)
    assert twice.summary["n_detections"] == stored
    ids = [e.episode_id for e in twice.episodes]
    assert len(ids) == len(set(ids)), "one row per episode"


def test_superseded_count_is_zero_on_a_clean_store(conn, edge_conn):
    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    pub = projection.load(path, audience=projection.AUDIENCE_PUBLIC)
    assert pub.summary["n_superseded"] == 0


def test_section_letters_are_unique_and_ordered(conn, edge_conn, tmp_path):
    """Social slots in as E, ahead of the trailing Beef panel.

    Regression: renaming Beef to F while also labelling social F produced a
    page with two F sections and no E.
    """
    import re

    from weatherwatch import report as weather_report

    _mixed_store(conn, edge_conn)
    path = edge_conn.execute("PRAGMA database_list").fetchone()[2]
    weather_report.generate_report(conn, tmp_path / "site", social_db=path,
                                   social_window=None)
    html = (tmp_path / "site" / "index.html").read_text()

    letters = re.findall(r"<h2>([A-Z]) &middot; |<h2>([A-Z]) · ", html)
    letters = [a or b for a, b in letters]
    assert letters, "the page must have lettered sections"
    assert len(letters) == len(set(letters)), f"duplicate letter: {letters}"
    assert letters == sorted(letters), f"letters out of order: {letters}"
    assert "E" in letters and "Social observations" in html
