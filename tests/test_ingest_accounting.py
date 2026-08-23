"""Controlled fault injection for every persisted ingest-accounting path."""

from __future__ import annotations

import json

import pytest

from weatherwatch.collector import Collector

BASE_US = 1_700_000_040_000_000
WIDTH_US = 60_000_000
ACCOUNTING = (
    "parse_errors", "rejected_no_time_us", "late_events", "unclassified",
)


def _event(time_us=BASE_US, *, kind="commit", collection="app.bsky.feed.post",
           operation="create"):
    return {
        "time_us": time_us,
        "kind": kind,
        "did": "did:example:fault-injection",
        "commit": {
            "collection": collection,
            "operation": operation,
            "rkey": "synthetic",
            "rev": "synthetic",
            "record": {"$type": collection},
        },
    }


def _collector(conn):
    return Collector(conn, "wss://relay.invalid/subscribe", bucket_width=60)


def _close_first(collector):
    collector._handle_raw(json.dumps(_event(BASE_US + WIDTH_US)))
    windows = collector.acc.take_closed()
    assert len(windows) == 1
    return windows[0]


def _assert_only(window, field, value=1):
    for name in ACCOUNTING:
        assert getattr(window, name) == (value if name == field else 0), (
            f"{name} changed while injecting {field}"
        )


def test_parse_failure_increments_only_parse_errors(conn):
    collector = _collector(conn)
    collector._handle_raw(json.dumps(_event()))
    collector._handle_raw('{"kind":')
    window = _close_first(collector)
    _assert_only(window, "parse_errors")
    assert window.events_seen == 1
    assert window.counts == {"post.create": 1}


def test_malformed_parsed_frame_is_accounted_without_crashing(conn):
    collector = _collector(conn)
    collector._handle_raw(json.dumps(_event()))
    collector._handle_raw(json.dumps(["not", "an", "envelope"]))
    window = _close_first(collector)
    _assert_only(window, "rejected_no_time_us")


def test_missing_timestamp_before_first_valid_frame_is_not_lost(conn):
    collector = _collector(conn)
    missing = _event()
    del missing["time_us"]
    collector._handle_raw(json.dumps(missing))
    collector._handle_raw(json.dumps(_event()))
    window = _close_first(collector)
    _assert_only(window, "rejected_no_time_us")
    assert window.events_seen == 1


@pytest.mark.parametrize(("mutation", "expected_metric"), [
    (lambda event: event.update(kind="future_kind"), "unclassified.kind"),
    (lambda event: event["commit"].update(operation="upsert"),
     "unclassified.operation"),
    (lambda event: event["commit"].update(collection="com.example.unknown"),
     "untracked.collection"),
    (lambda event: event["commit"].pop("collection"),
     "malformed.collection"),
    (lambda event: event.update(commit=[]), "malformed.commit"),
])
def test_unknown_and_malformed_schema_paths_are_bounded_and_exact(
        conn, mutation, expected_metric):
    collector = _collector(conn)
    collector._handle_raw(json.dumps(_event()))
    fault = _event(BASE_US + 1)
    mutation(fault)
    collector._handle_raw(json.dumps(fault))
    window = _close_first(collector)
    _assert_only(window, "unclassified")
    assert window.counts == {"post.create": 1, expected_metric: 1}
    assert window.events_seen == 2


def test_late_event_increments_only_late_counter_and_is_not_recounted(conn):
    collector = _collector(conn)
    collector._handle_raw(json.dumps(_event()))
    collector._handle_raw(json.dumps(_event(BASE_US + WIDTH_US)))
    first = collector.acc.take_closed()[0]
    _assert_only(first, "late_events", value=0)

    collector._handle_raw(json.dumps(_event(BASE_US + 1)))
    collector._handle_raw(json.dumps(_event(BASE_US + 2 * WIDTH_US)))
    second = collector.acc.take_closed()[0]
    _assert_only(second, "late_events")
    assert second.events_seen == 1
    assert second.counts == {"post.create": 1}


def test_ordinary_valid_input_leaves_all_accounting_zero(conn):
    collector = _collector(conn)
    collector._handle_raw(json.dumps(_event()))
    collector._handle_raw(json.dumps(_event(BASE_US + 1)))
    window = _close_first(collector)
    _assert_only(window, "parse_errors", value=0)
    assert window.events_seen == 2
    assert window.counts == {"post.create": 2}
