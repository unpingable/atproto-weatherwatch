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
        "time_bucket_seconds": projection.PUBLIC_TIME_BUCKET_S,
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
    assert doc["schema"] == "weatherwatch.social/v2"
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
    letters = re.findall(r"<h2>([A-Z]) &middot; |<h2>([A-Z]) · ", rendered)
    letters = [left or right for left, right in letters]
    assert letters and len(letters) == len(set(letters))
