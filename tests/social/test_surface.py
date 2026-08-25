"""Adversarial tests for the published social disclosure boundary."""

from __future__ import annotations

import json
import re

import pytest

from weatherwatch.social import api, episodes, projection, section
from weatherwatch.social.edges import StatusEvent
from weatherwatch.social.sensors import aggregate

from .conftest import BASE_US, build_run, edge as mk, write_edges, write_status

RUN = "run-surface"
SEC = 1_000_000
BURST_START_US = BASE_US + 20 * 60 * SEC

PUBLISH_GATE = re.compile(
    r"did:(plc|web|key):|at://|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)|\ba:[0-9a-f]{12}\b")


def _weather_episode(conn, metric="block.create"):
    windows = [
        {"metrics": {metric: 10 + (index % 3) - 1}}
        for index in range(20)
    ]
    windows += [{"metrics": {metric: 90}} for _ in range(4)]
    windows += [{"metrics": {metric: 10}} for _ in range(6)]
    build_run(conn, RUN, windows)
    return episodes.run_aggregate(
        conn, [RUN], [metric], None, None, aggregate.AggregateConfig())


def _support_edges(edge_conn, actors, collection="block", op="create",
                   start_us=BURST_START_US):
    rows = []
    for actor in range(actors):
        # Several events per actor makes event count intentionally different
        # from actor cardinality.
        for occurrence in range(3):
            rows.append(mk(
                f"did:plc:actor{actor:03d}",
                f"did:plc:target{occurrence:03d}",
                start_us + (actor * 3 + occurrence) * SEC,
                collection=collection, op=op,
            ))
    write_edges(edge_conn, rows)


def _public_path(edge_conn):
    return edge_conn.execute("PRAGMA database_list").fetchone()[2]


def _seed_public(conn, edge_conn, actors=projection.PUBLIC_MIN_ACTORS,
                 metric="block.create"):
    sealed = _weather_episode(conn, metric)
    collection, op = metric.split(".", 1)
    _support_edges(edge_conn, actors, collection=collection, op=op)
    episodes.persist(edge_conn, sealed)
    return projection.load(_public_path(edge_conn),
                           audience=projection.AUDIENCE_PUBLIC)


def _mixed_store(conn, edge_conn):
    sealed = _weather_episode(conn)
    _support_edges(edge_conn, projection.PUBLIC_MIN_ACTORS)
    # A separate local actor/target structure plus a lifecycle event ensures
    # all detector tiers coexist in one store.
    targets = [f"did:plc:shared{index:02d}" for index in range(10)]
    write_edges(edge_conn, [
        mk(f"did:plc:cohort{actor}", target,
           BASE_US + (actor * 5 + index) * SEC)
        for actor in range(8) for index, target in enumerate(targets)
    ])
    write_status(edge_conn, [StatusEvent(
        BASE_US + 100 * SEC, "did:plc:shared00", 0, "deactivated")])
    sealed += episodes.run_edge(edge_conn, ["block"], BASE_US,
                                BASE_US + 3600 * SEC)
    sealed += episodes.run_lifecycle(edge_conn, BASE_US,
                                     BASE_US + 3600 * SEC)
    episodes.persist(edge_conn, sealed)


def test_public_projection_is_reduced_while_local_audit_remains_complete(
        conn, edge_conn):
    _mixed_store(conn, edge_conn)
    path = _public_path(edge_conn)
    public = projection.load(path, audience=projection.AUDIENCE_PUBLIC)
    local = projection.load(path, audience=projection.AUDIENCE_LOCAL)

    assert public.available and local.available
    assert all(isinstance(row, projection.PublicEpisodeView)
               for row in public.episodes)
    assert {row.detector_id for row in local.episodes} >= {
        "aggregate_rate_episode", "edge_structure_episode",
        "account_lifecycle_episode",
    }


@pytest.mark.parametrize("actors", [1, 2, 3, 9])
def test_one_two_and_tiny_actor_cohorts_are_suppressed(conn, edge_conn, actors):
    public = _seed_public(conn, edge_conn, actors=actors)
    assert not public.available
    assert public.episodes == ()
    assert "disclosure policy" in public.reason


def test_event_volume_cannot_substitute_for_actor_cardinality(conn, edge_conn):
    sealed = _weather_episode(conn)
    # One actor creates many events during the exact episode interval.
    write_edges(edge_conn, [
        mk("did:plc:onlyactor", f"did:plc:target{index}",
           BURST_START_US + index * 1000)
        for index in range(200)
    ])
    episodes.persist(edge_conn, sealed)
    public = projection.load(_public_path(edge_conn),
                             audience=projection.AUDIENCE_PUBLIC)
    assert not public.available


def test_threshold_is_explicit_provisional_and_admits_its_limit(conn, edge_conn):
    public = _seed_public(conn, edge_conn)
    policy = public.source["disclosure_policy"]
    assert policy == {
        "minimum_distinct_actors": projection.PUBLIC_MIN_ACTORS,
        "minimum_distinct_actors_measures": (
            "ambient actor cardinality for the collection and operation "
            "during the interval, not the cardinality of the departure"),
        "excess_dominance_suppression": projection.DOMINANCE_GATE,
        "time_bucket_seconds": projection.PUBLIC_TIME_BUCKET_S,
        "time_coarsening_is_load_bearing": False,
        "exact_statistics_published": False,
        "stable_episode_identifiers_published": False,
        "claim": "disclosure resistance; not anonymity",
    }
    assert public.available
    assert {row.actor_support for row in public.episodes} == {
        f"{projection.PUBLIC_MIN_ACTORS}+"}


def test_distinctive_timing_is_coarsened_and_exact_timestamps_are_absent(
        conn, edge_conn):
    public = _seed_public(conn, edge_conn)
    local = projection.load(_public_path(edge_conn),
                            audience=projection.AUDIENCE_LOCAL)
    exact_starts = {row.ts_start for row in local.episodes}
    payload = public.as_dict()
    blob = json.dumps(payload, sort_keys=True)
    assert all(row.period_start.endswith(":00:00Z") for row in public.episodes)
    assert all(row.period_end.endswith(":00:00Z") for row in public.episodes)
    assert not any(stamp in blob for stamp in exact_starts)
    assert "ts_start" not in blob and "ts_end" not in blob


def test_exact_counts_statistics_shape_and_stable_ids_are_not_public(
        conn, edge_conn):
    public = _seed_public(conn, edge_conn)
    fields = set(projection.PublicEpisodeView.__dataclass_fields__)
    forbidden = {
        "det_id", "episode_id", "evidence_id", "receipt_hash", "config_hash",
        "events_in_episode", "rate_ratio", "peak_z", "mean_z",
        "baseline_rate_eps", "extreme_rate_eps", "rise_windows",
        "fall_windows", "peak_position", "n_windows", "metric",
        "location", "latitude", "longitude", "geography", "geo",
    }
    assert fields.isdisjoint(forbidden)
    assert forbidden.isdisjoint(public.episodes[0].as_dict())


def test_lifecycle_adjacent_to_a_burst_never_reaches_public(conn, edge_conn):
    _mixed_store(conn, edge_conn)
    public = projection.load(_public_path(edge_conn),
                             audience=projection.AUDIENCE_PUBLIC)
    blob = json.dumps(public.as_dict())
    assert "account_lifecycle_episode" not in blob
    assert "deactivation" not in blob


def test_widened_detector_allowlist_still_fails_closed(conn, edge_conn,
                                                       monkeypatch):
    _mixed_store(conn, edge_conn)
    monkeypatch.setattr(
        projection, "PUBLIC_DETECTORS",
        frozenset({"aggregate_rate_episode", "edge_structure_episode",
                   "account_lifecycle_episode"}),
    )
    public = projection.load(_public_path(edge_conn),
                             audience=projection.AUDIENCE_PUBLIC)
    assert public.available
    assert all(isinstance(row, projection.PublicEpisodeView)
               for row in public.episodes)
    assert not re.search(r"edge_structure|lifecycle|actor_token",
                         json.dumps([row.as_dict()
                                     for row in public.episodes]))


def test_unsupported_collection_and_missing_edge_evidence_fail_closed(
        conn, edge_conn):
    sealed = _weather_episode(conn, metric="post.delete")
    episodes.persist(edge_conn, sealed)
    public = projection.load(_public_path(edge_conn),
                             audience=projection.AUDIENCE_PUBLIC)
    assert not public.available


def test_repeated_rare_signatures_are_not_enumerated(conn, edge_conn):
    public = _seed_public(conn, edge_conn)
    assert len(public.episodes) == 1
    row = edge_conn.execute(
        "SELECT * FROM episode WHERE detector_id='aggregate_rate_episode'"
    ).fetchone()
    env = json.loads(row["envelope_json"])
    env["det_id"] = "det-clone"
    env["subject"]["value"] = "episode-clone"
    clone = dict(row)
    clone.update({
        "det_id": "det-clone", "subject_value": "episode-clone",
        "envelope_json": json.dumps(env), "sealed_at": "2099-01-01T00:00:00Z",
    })
    columns = list(clone)
    edge_conn.execute(
        f"INSERT INTO episode({','.join(columns)}) VALUES "
        f"({','.join('?' for _ in columns)})",
        [clone[column] for column in columns],
    )
    again = projection.load(_public_path(edge_conn),
                            audience=projection.AUDIENCE_PUBLIC)
    assert len(again.episodes) == 1
    assert "suppressed" not in json.dumps(again.summary).lower()


def test_public_projection_and_api_are_identity_free(conn, edge_conn, tmp_path):
    public = _seed_public(conn, edge_conn)
    out = api.write(public, tmp_path)
    doc = json.loads(out.read_text())
    assert doc["schema"] == "weatherwatch.social/v3"
    assert "not anonymity" in doc["disclaimer"]
    assert not PUBLISH_GATE.search(out.read_text())
    projection.assert_identity_free(doc)


def test_assert_identity_free_catches_identifier_shapes():
    for value in ("did:plc:abc", "did:web:example.com", "at://x/y",
                  "bafyreiabcdefghij", "someone.bsky.social",
                  "a:0123456789ab"):
        with pytest.raises(projection.IdentityLeak):
            projection.assert_identity_free({"leak": value})


def test_rendered_section_states_policy_and_retention(conn, edge_conn):
    public = _seed_public(conn, edge_conn)
    public = projection.SocialProjection(
        **{**public.__dict__, "sink_receipt": {
            "enabled": True, "collections": ["block", "listitem"],
            "retention": "24h", "config_source": "env",
            "config_hash": "abc",
        }}
    )
    rendered = section.render(public)
    assert "not anonymity" in rendered
    assert "fails\nclosed" in rendered or "fails closed" in rendered
    assert "exact counts" in rendered
    assert "block, listitem" in rendered and "24h" in rendered
    assert not PUBLISH_GATE.search(rendered)


def test_the_page_does_not_overclaim_what_the_actor_count_means(conn,
                                                               edge_conn):
    """A reader must not take `10+ distinct actors` for `ten accounts were
    involved in this episode`. That is not what is measured, and the page has
    to say which of the two it is."""
    rendered = section.render(_seed_public(conn, edge_conn))
    assert "acting in that period" in rendered
    assert "not how many produced the departure" in rendered
    # The wording the correction replaced must not come back.
    assert "distinct actors in the episode" not in rendered
    assert "distinct actors observed locally" not in rendered


def test_the_page_states_both_gates_and_the_limit_of_coarsening(conn,
                                                               edge_conn):
    rendered = section.render(_seed_public(conn, edge_conn))
    assert "no single actor emitted as many" in rendered
    assert "defence in depth" in rendered


def test_section_renders_unavailable_as_unknown_not_calm():
    empty = projection.SocialProjection(
        audience=projection.AUDIENCE_PUBLIC, available=False,
        reason="no episode store", source={"disclosure_policy": {}},
        sink_receipt={"enabled": False, "collections": [],
                      "retention": None, "config_source": "default",
                      "config_hash": "abc"},
    )
    rendered = section.render(empty).lower()
    assert "off" in rendered and "no episode store" in rendered
    assert "calm" not in rendered


def test_section_has_no_lookup_ranking_or_accusatory_surface(conn, edge_conn):
    rendered = section.render(_seed_public(conn, edge_conn)).lower()
    for shape in ("<input", "<form", "<select", "<button", "onclick=",
                  "fetch(", "leaderboard", "top blockers", "brigad",
                  "harass", "coordinat", "attack", "bad actor"):
        assert shape not in rendered
    assert "observation is not causation" in rendered


def test_section_letters_are_unique_and_ordered(conn, edge_conn, tmp_path):
    from weatherwatch import report as weather_report

    _mixed_store(conn, edge_conn)
    weather_report.generate_report(
        conn, tmp_path / "site", social_db=_public_path(edge_conn),
        social_window=None,
    )
    rendered = (tmp_path / "site" / "index.html").read_text()
    # The lettered sections are the receipts deck, and each is now the summary
    # of its own disclosure rather than an <h2>. The invariant is the letters,
    # not the tag they are wrapped in: every section keeps a distinct letter
    # and they run in order down the page.
    letters = re.findall(r"<h3>([A-Z]) &middot; |<h3>([A-Z]) · ", rendered)
    letters = [left or right for left, right in letters]
    assert letters and len(letters) == len(set(letters))
    assert letters == sorted(letters), f"sections out of order: {letters}"
    assert letters[0] == "A"


# --- what the cardinality floor does not do, demonstrated ------------------
#
# The floor counts distinct actors performing the same collection and
# operation anywhere in the observed stream during the episode interval. That
# is *ambient* cardinality. The tests above satisfy it with a purpose-built
# cohort; the network satisfies it with unrelated traffic. Below: the gap that
# creates, and the gate that closes it.

def _one_actor_excess(conn, edge_conn, ambient_actors=12, dominant_events=320):
    """An aggregate excess whose events came almost entirely from one actor,
    inside an interval that also carries unrelated ambient traffic."""
    sealed = _weather_episode(conn)
    rows = [
        mk("did:plc:dominantactor", f"did:plc:subject{index:04d}",
           BURST_START_US + index * 700)
        for index in range(dominant_events)
    ]
    rows += [
        mk(f"did:plc:ambient{index:02d}", f"did:plc:other{index:02d}",
           BURST_START_US + index * 9000)
        for index in range(ambient_actors)
    ]
    write_edges(edge_conn, rows)
    episodes.persist(edge_conn, sealed)
    return projection.load(_public_path(edge_conn),
                           audience=projection.AUDIENCE_PUBLIC)


def test_a_one_actor_excess_is_not_excluded_by_ambient_cardinality_alone(
        conn, edge_conn, monkeypatch):
    """The demonstration, with the second gate disabled.

    Twelve unrelated accounts each blocking once satisfies a ten-actor floor
    while one account produces the entire departure. The floor is not wrong,
    it is answering a different question than a reader assumes, and this is
    what that costs.
    """
    monkeypatch.setattr(
        projection, "_excess_is_distributed",
        lambda *args, **kwargs: True)
    public = _one_actor_excess(conn, edge_conn)
    assert public.available, (
        "cardinality alone admits it — this is the failure the next test "
        "closes, kept executable so the limitation cannot be forgotten")
    assert public.episodes[0].actor_support == \
        f"{projection.PUBLIC_MIN_ACTORS}+"


def test_an_excess_one_actor_could_account_for_is_suppressed(conn, edge_conn):
    """The gate, live. No threshold: the episode's own excess over baseline
    is the bar, and one actor clearing it alone means suppression."""
    public = _one_actor_excess(conn, edge_conn)
    assert not public.available
    assert public.episodes == ()
    assert "disclosure policy" in public.reason


def test_a_genuinely_distributed_excess_still_publishes(conn, edge_conn):
    """The gate must not simply suppress everything: an excess spread across
    many accounts, none of which could account for it alone, still reaches
    the public surface."""
    sealed = _weather_episode(conn)
    # 340 events spread over 40 actors: the busiest has 9, far below the
    # episode's excess over baseline.
    write_edges(edge_conn, [
        mk(f"did:plc:spread{index % 40:02d}", f"did:plc:subject{index:04d}",
           BURST_START_US + index * 700)
        for index in range(340)
    ])
    episodes.persist(edge_conn, sealed)
    public = projection.load(_public_path(edge_conn),
                             audience=projection.AUDIENCE_PUBLIC)
    assert public.available
    assert len(public.episodes) == 1


def test_the_dominance_gate_fails_closed_on_missing_statistics(
        conn, edge_conn):
    """"Cannot tell" is not "safe to publish". Without the episode's event
    count or baseline rate the excess is not computable, so it is suppressed
    rather than waved through."""
    sealed = _weather_episode(conn)
    write_edges(edge_conn, [
        mk(f"did:plc:spread{index % 40:02d}", f"did:plc:subject{index:04d}",
           BURST_START_US + index * 700)
        for index in range(340)
    ])
    episodes.persist(edge_conn, sealed)
    row = edge_conn.execute(
        "SELECT * FROM episode WHERE detector_id='aggregate_rate_episode'"
    ).fetchone()
    env = json.loads(row["envelope_json"])
    for stripped in ("events_in_episode", "baseline_rate_eps"):
        broken = dict(env)
        broken["explain"] = {k: v for k, v in env["explain"].items()
                             if k != stripped}
        edge_conn.execute(
            "UPDATE episode SET envelope_json=? WHERE det_id=?",
            (json.dumps(broken), row["det_id"]))
        public = projection.load(_public_path(edge_conn),
                                 audience=projection.AUDIENCE_PUBLIC)
        assert not public.available, f"{stripped} missing must suppress"


def test_a_deficit_episode_is_not_subjected_to_the_dominance_gate(
        conn, edge_conn):
    """A lull is an absence. No account can account for events that did not
    happen, so the excess comparison is meaningless there and is not applied.
    """
    windows = [{"metrics": {"block.create": 90 + (index % 3) - 1}}
               for index in range(20)]
    windows += [{"metrics": {"block.create": 8}} for _ in range(4)]
    windows += [{"metrics": {"block.create": 90}} for _ in range(6)]
    build_run(conn, RUN, windows)
    sealed = episodes.run_aggregate(
        conn, [RUN], ["block.create"], None, None, aggregate.AggregateConfig())
    _support_edges(edge_conn, projection.PUBLIC_MIN_ACTORS)
    episodes.persist(edge_conn, sealed)
    public = projection.load(_public_path(edge_conn),
                             audience=projection.AUDIENCE_PUBLIC)
    assert public.available
    assert {row.direction for row in public.episodes} == {"deficit"}


def test_hour_coarsening_is_not_what_hides_the_timing(conn, edge_conn,
                                                      tmp_path):
    """The published period is an hour; the same site publishes the metric
    that produced it at full 60-second resolution.

    `summary.json` and the primitive sparklines carry per-window marks for
    `block.create` over the whole interval, so an observer who reads the
    coarse episode row can recover the minute the rate peaked from the same
    page. Coarsening is therefore defence in depth, not the control that
    bounds reconstruction — the gates on actor support are. Asserting it here
    keeps the claim in `BOUNDARIES.md` honest.
    """
    from weatherwatch import report

    sealed = _weather_episode(conn)
    write_edges(edge_conn, [
        mk(f"did:plc:spread{index % 40:02d}", f"did:plc:subject{index:04d}",
           BURST_START_US + index * 700)
        for index in range(340)
    ])
    episodes.persist(edge_conn, sealed)
    # No trailing window: the synthetic epoch is years before "now", and the
    # question here is about what the page carries, not about recency.
    report.generate_report(conn, tmp_path / "site",
                           social_db=_public_path(edge_conn),
                           social_window=None)
    html = (tmp_path / "site" / "index.html").read_text()
    social = json.loads((tmp_path / "site" / "social.json").read_text())

    assert social["episodes"], "the episode must actually be published"
    assert all(row["period_start"].endswith(":00:00Z")
               for row in social["episodes"])
    # ...and the minute-resolution shape of the same metric is right there.
    assert "Blocks" in html
    strip_stamps = re.findall(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) —", html)
    minutes = {stamp for stamp in strip_stamps if not stamp.endswith(":00:00Z")}
    assert minutes, (
        "per-window timestamps are published; if this ever stops being true, "
        "the coarsening claim in BOUNDARIES.md can be strengthened")
    assert social["source"]["disclosure_policy"][
        "time_coarsening_is_load_bearing"] is False


def test_social_json_states_its_guarantees_machine_readably(conn, edge_conn,
                                                            tmp_path):
    """`social.json` is fetched on its own. A consumer that never opens the
    page must still be able to parse what the rows are about, and what they
    are not — the misread this answers was observed on the weather lane, where
    a reader given the page concluded conflict monitoring."""
    public = _seed_public(conn, edge_conn)
    doc = json.loads(api.write(public, tmp_path).read_text())

    assert doc["subject_types"] == ["episode"], (
        "the bounded subject set is the whole boundary; it must be readable "
        "without parsing prose")
    assert doc["instrument"]["id"] == "weatherwatch"
    assert doc["instrument"]["collector_version"]
    assert "aggregate ATProto events" in doc["measures"] or \
        "aggregate event" in doc["measures"]
    for absent in ("conflict or disputes", "individual users or accounts",
                   "coordination, intent, or motive", "any identity",
                   "geographic origin"):
        assert absent in doc["does_not_measure"]


def test_the_public_artifact_names_no_mechanism_or_intent(conn, edge_conn,
                                                          tmp_path):
    """`does_not_measure` may name what is absent; nothing else in the
    artifact may narrate a mechanism."""
    public = _seed_public(conn, edge_conn)
    doc = json.loads(api.write(public, tmp_path).read_text())
    doc.pop("does_not_measure")
    blob = json.dumps(doc).lower()
    for banned in ("coordinated", "brigad", "harass", "attack", "mob",
                   "bad actor", "toxic", "culprit", "responsible for"):
        assert banned not in blob, f"{banned!r} reached the public artifact"


def test_the_policy_is_stated_even_when_there_are_no_episodes(conn, tmp_path):
    """An empty artifact must still say what would have been applied. "No
    rows this time" and "no policy" are different facts."""
    from weatherwatch import report

    empty = projection.load(tmp_path / "absent.sqlite",
                            audience=projection.AUDIENCE_PUBLIC)
    assert not empty.available
    assert empty.source["disclosure_policy"] == \
        projection.public_disclosure_policy()

    # ...and by the route the deployed publisher actually takes, with no
    # episode store configured at all.
    unset = report._load_social(conn, None)
    assert unset.source["disclosure_policy"] == \
        projection.public_disclosure_policy()
    doc = api.build(unset)
    assert doc["source"]["disclosure_policy"]["minimum_distinct_actors"] == \
        projection.PUBLIC_MIN_ACTORS
    assert doc["subject_types"] == ["episode"]
