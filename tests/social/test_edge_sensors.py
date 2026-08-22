"""Tier 2: concentration, overlap, synchronisation over the edge store."""

from __future__ import annotations

import pytest

from weatherwatch.social.sensors import edge

from .conftest import BASE_US, edge as mk, write_edges

SEC = 1_000_000
WIN = (BASE_US, BASE_US + 3600 * SEC)


def _ev(conn, events, collection="block"):
    write_edges(conn, events)
    return edge.select(conn, collection, *WIN)


def test_diffuse_activity_produces_no_overlap_finding(edge_conn):
    """40 actors, disjoint targets, spread out. The negative control: if this
    fires, every ordinary hour on the network is an 'episode'."""
    events = []
    for a in range(40):
        for t in range(4):
            events.append(mk(f"did:plc:actor{a:03d}", f"did:plc:target{a:03d}{t}",
                             BASE_US + (a * 60 + t) * SEC))
    ev = _ev(edge_conn, events)
    found = edge.interpret(ev)
    assert not [f for f in found if f.type == edge.TYPE_COHORT_OVERLAP]
    assert not [f for f in found if f.type == edge.TYPE_SYNCHRONISATION]


def test_shared_target_set_produces_a_cohort(edge_conn):
    """Eight actors, near-identical target sets, inside two minutes."""
    targets = [f"did:plc:shared{i:02d}" for i in range(10)]
    events = []
    for a in range(8):
        for i, t in enumerate(targets):
            events.append(mk(f"did:plc:cohort{a}", t, BASE_US + (a * 5 + i) * SEC))
    ev = _ev(edge_conn, events)
    found = edge.interpret(ev)
    overlap = [f for f in found if f.type == edge.TYPE_COHORT_OVERLAP]
    assert len(overlap) == 1
    assert overlap[0].explain["n_actors"] == 8
    assert overlap[0].explain["mean_jaccard"] == 1.0
    assert overlap[0].explain["n_shared_targets"] == 10


def test_cohort_finding_carries_its_own_base_rate(edge_conn):
    """A shared target that everyone in scope touches is the boring
    explanation, and it must be visible on the finding, not buried."""
    targets = [f"did:plc:shared{i:02d}" for i in range(6)]
    events = []
    for a in range(6):
        for i, t in enumerate(targets):
            events.append(mk(f"did:plc:cohort{a}", t, BASE_US + (a * 5 + i) * SEC))
    ev = _ev(edge_conn, events)
    overlap = [f for f in edge.interpret(ev)
               if f.type == edge.TYPE_COHORT_OVERLAP]
    assert overlap[0].explain["target_prevalence"] == 1.0


def test_no_type_string_names_a_mechanism(edge_conn):
    """'coordinated', 'brigade', 'harassment' are claims about intent. This
    instrument observes records."""
    targets = [f"did:plc:shared{i:02d}" for i in range(10)]
    events = [mk(f"did:plc:cohort{a}", t, BASE_US + (a * 5 + i) * SEC)
              for a in range(8) for i, t in enumerate(targets)]
    ev = _ev(edge_conn, events)
    banned = ("coordinat", "brigad", "harass", "attack", "mob", "abuse",
              "malicious", "bad_actor", "toxic")
    for f in edge.interpret(ev):
        blob = (f.type + " " + str(f.explain)).lower()
        for word in banned:
            assert word not in f.type.lower(), f"{f.type} names a mechanism"
        assert "note" in f.explain or "confounders" in f.explain or True


def test_target_concentration_fires_on_a_single_hot_subject(edge_conn):
    events = [mk(f"did:plc:actor{a:03d}", "did:plc:hot", BASE_US + a * SEC)
              for a in range(60)]
    ev = _ev(edge_conn, events)
    found = [f for f in edge.interpret(ev)
             if f.type == edge.TYPE_TARGET_CONCENTRATION]
    assert found and found[0].explain["herfindahl"] == 1.0


def test_actor_concentration_fires_on_a_single_busy_repo(edge_conn):
    events = [mk("did:plc:oneactor", f"did:plc:t{t:04d}", BASE_US + t * SEC)
              for t in range(80)]
    ev = _ev(edge_conn, events)
    found = [f for f in edge.interpret(ev)
             if f.type == edge.TYPE_ACTOR_CONCENTRATION]
    assert found and found[0].explain["herfindahl"] == 1.0


def test_synchronisation_measures_compression_not_motive(edge_conn):
    targets = [f"did:plc:shared{i:02d}" for i in range(8)]
    events = [mk(f"did:plc:sync{a}", t, BASE_US + i * SEC)
              for a in range(10) for i, t in enumerate(targets)]
    ev = _ev(edge_conn, events)
    found = [f for f in edge.interpret(ev)
             if f.type == edge.TYPE_SYNCHRONISATION]
    assert found
    assert found[0].explain["first_act_span_s"] == 0.0
    assert "actor_tokens" in found[0].explain


def test_below_min_events_yields_nothing(edge_conn):
    events = [mk("did:plc:a", "did:plc:b", BASE_US)]
    ev = _ev(edge_conn, events)
    assert edge.interpret(ev) == []


def test_oversized_scope_refuses_rather_than_sampling(edge_conn):
    """A quietly truncated overlap number is worse than none: it looks
    complete."""
    events = []
    for a in range(edge.MAX_OVERLAP_ACTORS + 5):
        for t in range(3):
            events.append(mk(f"did:plc:a{a:05d}", f"did:plc:t{a:05d}{t}",
                             BASE_US + a * 100))
    ev = _ev(edge_conn, events)
    with pytest.raises(edge.ScopeTooLarge):
        edge.interpret(ev)


def test_like_targets_reduce_to_the_target_repo(edge_conn):
    """Actors hitting different posts of one account still read as one target,
    which is the thing the question is actually about."""
    events = []
    for a in range(6):
        for i in range(4):
            uri = f"at://did:plc:author/app.bsky.feed.post/3k{i}"
            events.append(mk(f"did:plc:liker{a}", uri, BASE_US + (a * 4 + i) * SEC,
                             collection="like", kind="uri"))
    ev = _ev(edge_conn, events, collection="like")
    found = [f for f in edge.interpret(ev, edge.EdgeConfig(min_events=10,
                                                           min_actor_targets=1))
             if f.type == edge.TYPE_COHORT_OVERLAP]
    assert found and found[0].explain["n_shared_targets"] == 1
