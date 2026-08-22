"""Edge extraction: every message yields an event or a counted reason."""

from __future__ import annotations

import pytest

from weatherwatch.social import edges


def commit(collection, record, op="create", did="did:plc:actor", t=1_000, **kw):
    c = {"rev": "r1", "operation": op, "collection": collection,
         "rkey": "3kabc", "cid": "bafy1"}
    if op != "delete":
        c["record"] = record
    c.update(kw)
    return {"did": did, "time_us": t, "kind": "commit", "commit": c}


def test_block_yields_did_edge():
    e = edges.extract(commit("app.bsky.graph.block",
                             {"subject": "did:plc:target",
                              "createdAt": "2026-01-01T00:00:00Z"}))
    assert isinstance(e, edges.EdgeEvent)
    assert (e.collection, e.subject_kind, e.subject_ref) == (
        "block", "did", "did:plc:target")
    assert e.record_created_at == "2026-01-01T00:00:00Z"


def test_like_yields_uri_edge_from_strongref():
    uri = "at://did:plc:author/app.bsky.feed.post/3kxyz"
    e = edges.extract(commit("app.bsky.feed.like",
                             {"subject": {"uri": uri, "cid": "bafy2"}}))
    assert e.subject_kind == "uri" and e.subject_ref == uri
    assert edges.subject_actor_did(e.subject_kind, e.subject_ref) == "did:plc:author"


def test_delete_is_retained_with_unknown_subject():
    """A withdrawal is an event. Dropping it would make unblocks invisible."""
    e = edges.extract(commit("app.bsky.graph.block", None, op="delete"))
    assert isinstance(e, edges.EdgeEvent)
    assert e.op == "delete"
    assert e.subject_kind == edges.SUBJECT_UNKNOWN
    assert e.subject_ref == "" and e.cid == ""


def test_account_status_event():
    msg = {"did": "did:plc:x", "time_us": 5, "kind": "account",
           "account": {"active": False, "status": "deactivated"}}
    e = edges.extract(msg)
    assert isinstance(e, edges.StatusEvent)
    assert e.active == 0 and e.status == "deactivated"


def test_unknown_status_buckets_to_other():
    msg = {"did": "did:plc:x", "time_us": 5, "kind": "account",
           "account": {"active": False, "status": "quarantined"}}
    assert edges.extract(msg).status == "other"


def test_post_is_untracked_not_an_error():
    e = edges.extract(commit("app.bsky.feed.post", {"text": "hello"}))
    assert isinstance(e, edges.Skipped)
    assert e.reason == edges.SKIP_UNTRACKED


def test_identity_event_is_not_a_commit():
    e = edges.extract({"did": "did:plc:x", "time_us": 1, "kind": "identity"})
    assert isinstance(e, edges.Skipped) and e.reason == edges.SKIP_NOT_COMMIT


@pytest.mark.parametrize("msg", [
    None, 42, "string", [], {},
    {"kind": "commit"},
    {"did": "did:plc:x", "kind": "commit"},
    {"did": "did:plc:x", "time_us": "not-an-int", "kind": "commit"},
    {"did": "", "time_us": 1, "kind": "commit", "commit": {}},
    {"did": "did:plc:x", "time_us": 1, "kind": "account"},
    {"did": "did:plc:x", "time_us": 1, "kind": "commit", "commit": "nope"},
])
def test_hostile_input_never_raises(msg):
    """An observer that dies on a bad message has a hole it cannot report."""
    out = edges.extract(msg)
    assert isinstance(out, (edges.Skipped, edges.EdgeEvent, edges.StatusEvent))


def test_block_with_non_did_subject_is_no_subject():
    e = edges.extract(commit("app.bsky.graph.block", {"subject": "not-a-did"}))
    assert isinstance(e, edges.Skipped) and e.reason == edges.SKIP_NO_SUBJECT


def test_like_with_missing_uri_is_no_subject():
    e = edges.extract(commit("app.bsky.feed.like", {"subject": {"cid": "x"}}))
    assert isinstance(e, edges.Skipped) and e.reason == edges.SKIP_NO_SUBJECT


def test_tracked_aliases_are_a_subset_of_the_weather_vocabulary():
    """The two lanes must not drift: everything retained here is a collection
    the weather lane also knows, so a mismatch is visible rather than silent."""
    from weatherwatch.classify import COLLECTION_ALIASES
    assert edges.TRACKED_ALIASES <= set(COLLECTION_ALIASES.values())


def test_extract_on_live_fixtures_never_raises(all_fixtures):
    for f in all_fixtures:
        out = edges.extract(f["event"] if "event" in f else f)
        assert isinstance(out, (edges.Skipped, edges.EdgeEvent, edges.StatusEvent))
