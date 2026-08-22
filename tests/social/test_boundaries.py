"""The boundaries, as executable assertions.

These are not style checks. Each one is a property the design claims in prose
somewhere, restated so that violating it breaks the build instead of breaking
the claim quietly.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

from weatherwatch import db as weather_db
from weatherwatch.social import edges, episodes, store
from weatherwatch.social.envelope import (
    VALID_SUBJECT_TYPES,
    envelope_to_dict,
    stable_json,
    validate_envelope,
)
from weatherwatch.social.scope import AnalysisConfig, Scope, seal
from weatherwatch.social.sensors import aggregate, edge, lifecycle

from .conftest import BASE_US, build_run, edge as mk, steady_then_burst, write_edges

RUN = "run-bound"
SEC = 1_000_000


def _agg_evidence(conn, windows=None):
    build_run(conn, RUN, windows or steady_then_burst(n_burst=4))
    return aggregate.select(conn, [RUN], "block.create", endpoint="ep")


def _edge_evidence(conn):
    targets = [f"did:plc:shared{i:02d}" for i in range(10)]
    write_edges(conn, [mk(f"did:plc:cohort{a}", t, BASE_US + (a * 5 + i) * SEC)
                       for a in range(8) for i, t in enumerate(targets)])
    return edge.select(conn, "block", BASE_US, BASE_US + 3600 * SEC)


# --- 1. repeated events create episodes ------------------------------------

def test_repeated_events_create_episodes(conn):
    ev = _agg_evidence(conn)
    found = aggregate.interpret(ev, aggregate.AggregateConfig(detect_lulls=False))
    assert found, "a sustained departure must produce an episode"
    assert all(len(f.segment_receipts) >= 2 for f in found)


# --- 2. unrelated events do not merge --------------------------------------

def test_different_metrics_never_merge_into_one_episode(conn):
    """Two event classes surging in the same minute are two episodes. A single
    merged 'incident' would be a narrative the records do not contain."""
    windows = []
    for i in range(20):
        windows.append({"metrics": {"block.create": 10 + (i % 3) - 1,
                                    "like.create": 10 + (i % 3) - 1}})
    for _ in range(4):
        windows.append({"metrics": {"block.create": 90, "like.create": 90}})
    for i in range(6):
        windows.append({"metrics": {"block.create": 10, "like.create": 10}})
    build_run(conn, RUN, windows)

    sealed = episodes.run_aggregate(
        conn, [RUN], ["block.create", "like.create"], None, None,
        aggregate.AggregateConfig(detect_lulls=False))
    envs = [e for e, _ in sealed]
    types = {e.type for e in envs}
    assert types == {"block_burst", "like_storm"}
    assert len({e.subject.value for e in envs}) == 2, "distinct episode identities"


def test_co_occurrence_reports_overlap_without_merging(conn):
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
        aggregate.AggregateConfig(detect_lulls=False))
    envs = [e for e, _ in sealed]
    pairs = episodes.co_occurrence(envs)

    assert pairs, "simultaneous episodes should be reported as co-occurring"
    assert len(envs) == 2, "co-occurrence must not reduce the episode count"
    for p in pairs:
        assert p["a_type"] != p["b_type"]
        assert "score" not in p and "cause" not in p


def test_unrelated_edge_actors_do_not_form_a_cohort(edge_conn):
    write_edges(edge_conn, [
        mk(f"did:plc:actor{a:03d}", f"did:plc:target{a:03d}{t}",
           BASE_US + (a * 60 + t) * SEC)
        for a in range(40) for t in range(4)
    ])
    ev = edge.select(edge_conn, "block", BASE_US, BASE_US + 3600 * SEC)
    found = edge.interpret(ev)
    assert not [f for f in found if f.type == edge.TYPE_COHORT_OVERLAP]


# --- 3. config changes config_hash, not evidence identity ------------------

def test_changing_config_moves_config_hash_only(conn):
    ev = _agg_evidence(conn)
    c1 = aggregate.AggregateConfig(z_peak=3.0, detect_lulls=False)
    c2 = aggregate.AggregateConfig(z_peak=2.5, detect_lulls=False)

    f1 = aggregate.interpret(ev, c1)
    f2 = aggregate.interpret(ev, c2)
    assert f1 and f2

    e1 = seal("d", "v1", ev, f1[0], c1)
    e2 = seal("d", "v1", ev, f2[0], c2)

    # Evidence identity is untouched...
    assert ev.evidence_id == ev.evidence_id
    assert e1.explain["evidence_id"] == e2.explain["evidence_id"]
    assert e1.explain["scope_id"] == e2.explain["scope_id"]
    scope_stub = ev.scope_stub()
    assert scope_stub in e1.evidence and scope_stub in e2.evidence
    # ...and the same segment yields the same episode identity...
    assert f1[0].segment_receipts == f2[0].segment_receipts
    assert e1.subject.value == e2.subject.value
    # ...while the interpretation is visibly different.
    assert e1.config_hash != e2.config_hash
    assert e1.receipt_hash != e2.receipt_hash


def test_evidence_id_is_stable_across_repeated_selection(conn):
    ev1 = _agg_evidence(conn)
    ev2 = aggregate.select(conn, [RUN], "block.create", endpoint="ep")
    assert ev1.evidence_id == ev2.evidence_id
    assert ev1.receipts == ev2.receipts


def test_different_evidence_yields_a_different_evidence_id(conn):
    ev1 = _agg_evidence(conn)
    ev2 = aggregate.select(conn, [RUN], "like.create", endpoint="ep")
    assert ev1.evidence_id != ev2.evidence_id


# --- 4. evidence stays separate from interpretation ------------------------

@pytest.mark.parametrize("module", [aggregate, edge, lifecycle])
def test_selection_cannot_see_a_config_and_interpretation_cannot_see_a_source(
    module,
):
    """The discipline is enforced by signature, not by convention.

    `select` takes no config, so thresholds cannot steer what is looked at.
    `interpret` takes no connection, so a conclusion cannot go back for more
    data than its receipt commits to.
    """
    sel = inspect.signature(module.select).parameters
    assert not any("config" in p or "cfg" in p for p in sel), module.__name__

    interp = inspect.signature(module.interpret).parameters
    assert list(interp)[0] == "evidence"
    assert not any(p in ("conn", "connection", "store", "source", "db")
                   for p in interp), module.__name__


def test_scope_holds_no_thresholds():
    assert set(Scope.__dataclass_fields__) == {
        "kind", "subject_class", "ts_start", "ts_end", "window", "source"}


def test_every_config_is_an_analysis_config():
    for cfg in (aggregate.AggregateConfig(), edge.EdgeConfig(),
                lifecycle.LifecycleConfig()):
        assert isinstance(cfg, AnalysisConfig)
        assert cfg.config_hash and len(cfg.config_hash) == 16


def test_evidence_covers_more_than_the_finding(conn):
    ev = _agg_evidence(conn)
    f = aggregate.interpret(ev, aggregate.AggregateConfig(detect_lulls=False))[0]
    assert set(f.segment_receipts) < set(ev.receipts)


# --- 5. the subject is an episode, never an account ------------------------

def _all_envelopes(conn, edge_conn):
    build_run(conn, RUN, steady_then_burst(n_burst=4))
    out = episodes.run_aggregate(
        conn, [RUN], ["block.create"], None, None,
        aggregate.AggregateConfig(detect_lulls=False))
    _edge_evidence(edge_conn)
    out += episodes.run_edge(
        edge_conn, ["block"], BASE_US, BASE_US + 3600 * SEC)
    from weatherwatch.social.edges import StatusEvent
    from .conftest import write_status
    write_status(edge_conn, [StatusEvent(BASE_US + 100 * SEC,
                                         "did:plc:shared00", 0, "deactivated")])
    out += episodes.run_lifecycle(
        edge_conn, BASE_US, BASE_US + 3600 * SEC)
    return [e for e, _ in out]


def test_every_subject_is_an_episode(conn, edge_conn):
    envs = _all_envelopes(conn, edge_conn)
    assert envs
    for e in envs:
        assert e.subject.type == "episode", f"{e.type} claims a {e.subject.type}"


def test_did_subject_type_exists_but_is_never_produced_here(conn, edge_conn):
    """The vendored vocabulary still permits it -- driftwatch uses it. This
    package must simply never reach for it."""
    assert "did" in VALID_SUBJECT_TYPES
    assert "episode" in VALID_SUBJECT_TYPES
    for e in _all_envelopes(conn, edge_conn):
        assert e.subject.type != "did"


IDENTITY_MARKERS = ("did:plc:", "did:web:", "at://", "bsky.social", "@")


def test_no_envelope_ever_serialises_an_identifier(conn, edge_conn):
    """The strongest available guarantee, and it is structural: identity is
    not on the envelope, so the renderer could not print one if it tried."""
    for e in _all_envelopes(conn, edge_conn):
        blob = stable_json(envelope_to_dict(e))
        for marker in IDENTITY_MARKERS:
            assert marker not in blob, f"{e.type} leaked {marker!r}"


def test_actor_tokens_are_salted_per_store(tmp_path):
    """Unsalted, a DID hash is reversible by anyone willing to enumerate the
    DID space -- which is public. Two stores must not be joinable by token."""
    a = store.connect(tmp_path / "a.sqlite")
    b = store.connect(tmp_path / "b.sqlite")
    store.init_db(a)
    store.init_db(b)
    did = "did:plc:example"
    assert (store.actor_token(store.token_salt(a), did)
            != store.actor_token(store.token_salt(b), did))
    assert (store.actor_token(store.token_salt(a), did)
            == store.actor_token(store.token_salt(a), did))
    a.close()
    b.close()


def test_all_envelopes_validate(conn, edge_conn):
    for e in _all_envelopes(conn, edge_conn):
        assert validate_envelope(e, strict=True) == []


# --- 6. retention posture ---------------------------------------------------

def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


def test_edge_store_has_no_dossier_table(edge_conn):
    """A per-actor rollup is a dossier row whatever it is called. The schema
    is an allowlist so adding one is a visible act."""
    assert _tables(edge_conn) == set(store.ALLOWED_TABLES)


def test_no_table_has_a_content_or_profile_column(edge_conn):
    for table in _tables(edge_conn):
        for row in edge_conn.execute(f"PRAGMA table_info({table})"):
            name = row[1].lower()
            for bad in store.FORBIDDEN_COLUMN_SUBSTRINGS:
                assert bad not in name, f"{table}.{row[1]} looks like {bad!r}"


def test_edge_event_columns_are_exactly_the_declared_minimum(edge_conn):
    cols = {r[1] for r in edge_conn.execute("PRAGMA table_info(edge_event)")}
    assert cols == {
        "observed_us", "actor_did", "collection", "op", "subject_kind",
        "subject_ref", "rkey", "rev", "cid", "record_created_at",
    }


def test_post_text_is_not_retainable_because_posts_are_not_tracked():
    assert "post" not in edges.TRACKED_ALIASES
    e = edges.extract({
        "did": "did:plc:x", "time_us": 1, "kind": "commit",
        "commit": {"rev": "r", "operation": "create",
                   "collection": "app.bsky.feed.post", "rkey": "k",
                   "cid": "c", "record": {"text": "secret"}},
    })
    assert isinstance(e, edges.Skipped)


def test_social_lane_never_writes_to_the_weather_database(conn, edge_conn):
    """Two postures, two files. The deployed page's guarantee depends on it."""
    before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    build_run(conn, RUN, steady_then_burst(n_burst=4))
    snapshot = json.dumps([
        dict(r) for r in conn.execute(
            "SELECT * FROM bucket ORDER BY bucket_start, metric")
    ], sort_keys=True)

    sealed = episodes.run_aggregate(
        conn, [RUN], ["block.create"], None, None,
        aggregate.AggregateConfig(detect_lulls=False))
    episodes.persist(edge_conn, sealed)

    after = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    assert before == after, "social lane added a table to the weather DB"
    assert snapshot == json.dumps([
        dict(r) for r in conn.execute(
            "SELECT * FROM bucket ORDER BY bucket_start, metric")
    ], sort_keys=True)
    assert "episode" not in _tables(conn)
    assert _tables(edge_conn) == set(store.ALLOWED_TABLES)


def test_weather_classifier_output_alphabet_is_untouched():
    """The weather lane's own guarantee: a DID cannot appear in its output
    because no DID is a member of a finite metric set. Restated here so a
    change to `classify` for this package's benefit fails loudly."""
    from weatherwatch.classify import ALLOWED_METRICS
    assert all(not m.startswith("did:") and "at://" not in m
               for m in ALLOWED_METRICS)
    assert len(ALLOWED_METRICS) < 200


def test_sink_drops_are_counted_not_absorbed(edge_conn):
    """Ported scar: naive ingest sheds 30-40% while health reports ok."""
    w = store.EdgeWriter(edge_conn, "run-x", batch_rows=10_000)
    for i in range(store.MAX_BUFFER_ROWS + 50):
        w.add_edge(mk(f"did:plc:a{i}", "did:plc:t", BASE_US + i))
    snap = w.health_snapshot()
    assert snap["dropped_backpressure"] == 50
    assert snap["seen"] == store.MAX_BUFFER_ROWS + 50
    w.flush(BASE_US)
    row = edge_conn.execute(
        "SELECT dropped_backpressure FROM sink_health").fetchone()
    assert row["dropped_backpressure"] == 50
