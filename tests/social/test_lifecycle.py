"""Account lifecycle transitions alongside observed inbound activity."""

from __future__ import annotations

from weatherwatch.social.edges import StatusEvent
from weatherwatch.social.sensors import lifecycle

from .conftest import BASE_US, edge as mk, write_edges, write_status

SEC = 1_000_000
HOUR = 3600 * SEC
T_OFF = BASE_US + 24 * HOUR
WIN = (BASE_US + 23 * HOUR, BASE_US + 25 * HOUR)

TARGET = "did:plc:leaves"


def _setup(conn, n_inbound: int, n_actors: int, n_baseline: int = 0):
    events = []
    for i in range(n_inbound):
        events.append(mk(f"did:plc:in{i % n_actors:03d}", TARGET,
                         T_OFF - (i + 1) * 60 * SEC))
    for i in range(n_baseline):
        events.append(mk(f"did:plc:old{i:03d}", TARGET,
                         T_OFF - 8 * HOUR - i * 60 * SEC))
    write_edges(conn, events)
    write_status(conn, [StatusEvent(T_OFF, TARGET, 0, "deactivated")])


def test_transition_with_inbound_excess_is_flagged(edge_conn):
    _setup(edge_conn, n_inbound=40, n_actors=12)
    ev = lifecycle.select(edge_conn, *WIN)
    found = lifecycle.interpret(ev)
    assert len(found) == 1
    f = found[0]
    assert f.type == lifecycle.TYPE_DEACTIVATION_AFTER_INBOUND
    assert f.explain["n_inbound_in_lookback"] == 40
    assert f.explain["n_distinct_inbound_actors"] == 12
    assert f.explain["status"] == "deactivated"


def test_quiet_deactivation_is_emitted_as_the_negative_case(edge_conn):
    """Without the negative case a co-occurrence rate reads as a mechanism."""
    _setup(edge_conn, n_inbound=1, n_actors=1)
    found = lifecycle.interpret(lifecycle.select(edge_conn, *WIN))
    assert len(found) == 1
    assert found[0].type == lifecycle.TYPE_DEACTIVATION_QUIET
    assert found[0].score == 0.0


def test_high_baseline_traffic_suppresses_the_excess_claim(edge_conn):
    """A popular account has inbound traffic all the time. The lookback count
    alone would flag every deactivation on the network."""
    _setup(edge_conn, n_inbound=40, n_actors=12, n_baseline=400)
    found = lifecycle.interpret(lifecycle.select(edge_conn, *WIN))
    assert found[0].type == lifecycle.TYPE_DEACTIVATION_QUIET


def test_finding_carries_its_confounders(edge_conn):
    _setup(edge_conn, n_inbound=40, n_actors=12)
    f = lifecycle.interpret(lifecycle.select(edge_conn, *WIN))[0]
    assert "PDS migration" in f.explain["confounders"]
    assert "Not a causal claim" in f.explain["note"]
    assert f.explain["lookback_s"] == lifecycle.DEFAULT_LOOKBACK_S


def test_lookback_is_committed_to_by_evidence_identity(edge_conn):
    """Lookback shapes what is looked at, so it belongs to the scope. Two
    lookbacks are two different pieces of evidence, not one re-scored."""
    _setup(edge_conn, n_inbound=40, n_actors=12)
    a = lifecycle.select(edge_conn, *WIN, lookback_s=3600)
    b = lifecycle.select(edge_conn, *WIN, lookback_s=7200)
    assert a.evidence_id != b.evidence_id
    assert "lookback=3600s" in a.scope.subject_class


def test_inbound_to_a_post_uri_counts_toward_its_author(edge_conn):
    events = [
        mk(f"did:plc:liker{i:03d}",
           f"at://{TARGET}/app.bsky.feed.post/3k{i}",
           T_OFF - (i + 1) * 60 * SEC, collection="like", kind="uri")
        for i in range(30)
    ]
    write_edges(edge_conn, events)
    write_status(edge_conn, [StatusEvent(T_OFF, TARGET, 0, "deactivated")])
    f = lifecycle.interpret(lifecycle.select(edge_conn, *WIN))[0]
    assert f.explain["n_inbound_in_lookback"] == 30
    assert f.explain["inbound_by_collection"] == {"like": 30}


def test_active_accounts_are_not_examined(edge_conn):
    write_status(edge_conn, [StatusEvent(T_OFF, "did:plc:fine", 1, "")])
    ev = lifecycle.select(edge_conn, *WIN)
    assert ev.facts["n_transitions"] == 0
    assert lifecycle.interpret(ev) == []


def test_zero_baseline_ratio_is_flagged_as_undefined(edge_conn):
    """With no prior traffic there is no ratio, only a count. The fallback is
    kept so the case stays visible, and flagged so it does not read as one."""
    _setup(edge_conn, n_inbound=40, n_actors=12, n_baseline=0)
    f = lifecycle.interpret(lifecycle.select(edge_conn, *WIN))[0]
    assert f.explain["excess_ratio_undefined_baseline"] is True
    assert f.explain["baseline_inbound_normalised"] == 0.0

    _setup(edge_conn, n_inbound=40, n_actors=12, n_baseline=40)
    f2 = lifecycle.interpret(lifecycle.select(edge_conn, *WIN))[0]
    assert f2.explain["excess_ratio_undefined_baseline"] is False
