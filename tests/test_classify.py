"""Classifier semantics. Every expectation here traces to an M0 measurement."""

from __future__ import annotations

import json

import pytest

from weatherwatch.classify import classify
from tests.conftest import by_shape

TS = 1_700_000_000_000_000


def commit(collection, operation, record=None, **extra):
    c = {"collection": collection, "operation": operation,
         "rev": "synthrev0000001", "rkey": "synthrkey0000001"}
    if record is not None:
        c["record"] = record
        c["cid"] = "bafysynthetic000000000000000000000000000001"
    c.update(extra)
    return {"kind": "commit", "time_us": TS,
            "did": "did:example:synth0000000000000001", "commit": c}


# --- deletes: collection only ---------------------------------------------

def test_delete_classified_by_collection_only():
    """M0: 5,830/5,830 deletes carried collection+rkey+rev and no record."""
    c = classify(commit("app.bsky.feed.post", "delete"))
    assert c.metrics == ("post.delete",)


def test_delete_emits_no_shape_metrics_even_if_a_record_appears():
    """Negative fixture: never observed live, but the classifier must not
    start inferring reply/quote shape from a delete if one ever shows up."""
    c = classify(commit("app.bsky.feed.post", "delete",
                        record={"$type": "app.bsky.feed.post",
                                "reply": {"parent": {}}}))
    assert c.metrics == ("post.delete",)
    assert not any("reply" in m or "quote" in m for m in c.metrics)


@pytest.mark.parametrize("collection,alias", [
    ("app.bsky.feed.like", "like"),
    ("app.bsky.feed.repost", "repost"),
    ("app.bsky.graph.follow", "follow"),
    ("app.bsky.graph.block", "block"),
    ("app.bsky.graph.listitem", "listitem"),
])
def test_delete_per_collection(collection, alias):
    assert classify(commit(collection, "delete")).metrics == (f"{alias}.delete",)


# --- post update is not post create ---------------------------------------

def test_post_update_is_distinct_from_create():
    """M0 observed 21 real post updates against 22,590 creates. Folding them
    together would silently inflate the create rate."""
    rec = {"$type": "app.bsky.feed.post", "createdAt": "2020-01-01T00:00:00.000Z"}
    created = classify(commit("app.bsky.feed.post", "create", record=rec))
    updated = classify(commit("app.bsky.feed.post", "update", record=rec))
    assert "post.create" in created.metrics
    assert "post.create" not in updated.metrics
    assert "post.update" in updated.metrics


def test_post_update_still_gets_shape_metrics():
    rec = {"$type": "app.bsky.feed.post",
           "reply": {"parent": {"uri": "at://x"}, "root": {"uri": "at://x"}}}
    c = classify(commit("app.bsky.feed.post", "update", record=rec))
    assert set(c.metrics) == {"post.update", "post.update.reply"}


# --- quote classification --------------------------------------------------

def test_quote_from_embed_record():
    rec = {"$type": "app.bsky.feed.post",
           "embed": {"$type": "app.bsky.embed.record", "record": {}}}
    m = classify(commit("app.bsky.feed.post", "create", record=rec)).metrics
    assert "post.create.quote" in m
    assert "post.create.embed.record" in m


def test_quote_from_record_with_media():
    rec = {"$type": "app.bsky.feed.post",
           "embed": {"$type": "app.bsky.embed.recordWithMedia",
                     "media": {"$type": "app.bsky.embed.images"}}}
    m = classify(commit("app.bsky.feed.post", "create", record=rec)).metrics
    assert "post.create.quote" in m
    assert "post.create.embed.recordWithMedia" in m


@pytest.mark.parametrize("etype,alias", [
    ("app.bsky.embed.images", "images"),
    ("app.bsky.embed.external", "external"),
    ("app.bsky.embed.video", "video"),
    ("app.bsky.embed.gallery", "gallery"),
])
def test_non_quote_embeds_are_never_quotes(etype, alias):
    """`app.bsky.embed.gallery` was found by M0 and is absent from the design
    candidate. It is media. If a future embed type is added to the alias table
    it must not become a quote by default."""
    rec = {"$type": "app.bsky.feed.post", "embed": {"$type": etype}}
    m = classify(commit("app.bsky.feed.post", "create", record=rec)).metrics
    assert f"post.create.embed.{alias}" in m
    assert "post.create.quote" not in m


def test_gallery_from_live_fixture_is_not_a_quote(live_fixtures):
    galleries = [
        f for f in live_fixtures
        if "embed=app.bsky.embed.gallery" in (f.get("_shape") or "")
    ]
    assert galleries, "expected at least one gallery fixture from the M0 corpus"
    for f in galleries:
        m = classify(f["event"]).metrics
        assert "post.create.quote" not in m and "post.update.quote" not in m
        assert any(x.endswith(".embed.gallery") for x in m)


def test_unknown_embed_type_falls_to_other_and_is_not_a_quote():
    rec = {"$type": "app.bsky.feed.post",
           "embed": {"$type": "app.bsky.embed.somethingNew"}}
    m = classify(commit("app.bsky.feed.post", "create", record=rec)).metrics
    assert "post.create.embed.other" in m
    assert "post.create.quote" not in m


def test_embed_missing_dollar_type_does_not_crash_or_become_a_quote(
    synthetic_fixtures,
):
    ev = by_shape(synthetic_fixtures, "synthetic|embed_missing_type")
    m = classify(ev).metrics
    assert "post.create.embed.other" in m
    assert "post.create.quote" not in m


def test_reply_and_quote_counted_in_both_families(synthetic_fixtures):
    ev = by_shape(synthetic_fixtures, "synthetic|reply_and_quote")
    m = classify(ev).metrics
    assert "post.create" in m
    assert "post.create.reply" in m
    assert "post.create.quote" in m


# --- unknown / malformed ---------------------------------------------------

def test_unknown_collection_counted_not_crashed():
    c = classify(commit("com.example.novel.lexicon", "create",
                        record={"$type": "com.example.novel.lexicon"}))
    assert c.metrics == ("unclassified.collection",)


def test_unknown_collection_payload_is_not_retained():
    c = classify(commit("com.example.novel.lexicon", "create",
                        record={"$type": "x", "secret": "value"}))
    assert "secret" not in repr(c) and "value" not in repr(c)


def test_unknown_operation_counted():
    assert classify(commit("app.bsky.feed.post", "upsert")).metrics == (
        "unclassified.operation",)


def test_unknown_kind_counted():
    assert classify({"kind": "sync", "time_us": TS}).metrics == (
        "unclassified.kind",)


def test_missing_time_us_is_rejected_not_bucketed(synthetic_fixtures):
    ev = by_shape(synthetic_fixtures, "synthetic|missing_time_us")
    assert classify(ev) is None


@pytest.mark.parametrize("shape", [
    "synthetic|time_us_string",
    "synthetic|time_us_negative",
])
def test_bad_time_us_rejected(synthetic_fixtures, shape):
    assert classify(by_shape(synthetic_fixtures, shape)) is None


@pytest.mark.parametrize("shape", [
    "synthetic|empty",
    "synthetic|null_kind",
    "synthetic|commit_scalar",
    "synthetic|record_null",
    "synthetic|record_list",
    "synthetic|no_collection",
    "synthetic|rwm_missing_media",
    "synthetic|reply_partial",
    "synthetic|createdAt_year_2999",
    "synthetic|createdAt_year_1970",
    "synthetic|createdAt_garbage",
])
def test_hostile_shapes_do_not_crash(synthetic_fixtures, shape):
    classify(by_shape(synthetic_fixtures, shape))  # must not raise


def test_created_at_never_influences_the_result(synthetic_fixtures):
    """Window assignment uses time_us only. A year-2999 createdAt — a real
    driftwatch production value — must change nothing."""
    scarred = by_shape(synthetic_fixtures, "synthetic|createdAt_year_2999")
    c = classify(scarred)
    assert c.time_us == scarred["time_us"]
    assert "2999" not in repr(c)


def test_malformed_json_lines_are_the_callers_problem(malformed_lines):
    """classify never sees these; they fail at json.loads. Assert that
    assumption holds so the parse_errors bucket stays meaningful."""
    for line in malformed_lines:
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        classify(parsed)  # parsed but non-dict / odd: must not raise


# --- identity / account ----------------------------------------------------

def test_identity_event_yields_a_count_and_nothing_else(synthetic_fixtures):
    for shape in ("synthetic|identity_with_handle", "synthetic|identity_no_handle"):
        c = classify(by_shape(synthetic_fixtures, shape))
        assert c.metrics == ("identity.event",)


def test_account_active_and_status(synthetic_fixtures):
    c = classify(by_shape(synthetic_fixtures, "synthetic|account_inactive_status"))
    assert set(c.metrics) == {
        "account.event", "account.active.false", "account.status.deactivated"}


def test_account_unknown_status_falls_to_other(synthetic_fixtures):
    c = classify(by_shape(synthetic_fixtures, "synthetic|account_unknown_status"))
    assert "account.status.other" in c.metrics


def test_account_missing_active(synthetic_fixtures):
    c = classify(by_shape(synthetic_fixtures, "synthetic|account_missing_active"))
    assert "account.active.unknown" in c.metrics


# --- the whole corpus ------------------------------------------------------

def test_live_corpus_classifies_with_low_unclassified_rate(live_fixtures):
    total = unclassified = 0
    for f in live_fixtures:
        c = classify(f["event"])
        if c is None:
            continue
        total += 1
        if any(m.startswith("unclassified.") for m in c.metrics):
            unclassified += 1
    assert total > 250
    # The corpus is shape-deduplicated, so third-party lexicons are heavily
    # over-represented relative to the live stream (~2%). This bound only
    # asserts the classifier handles the bsky vocabulary it claims to.
    assert unclassified < total * 0.5
