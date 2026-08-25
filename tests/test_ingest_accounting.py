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


# --- the canary must not become the leak -----------------------------------

NOVEL_NSID = "com.example.novel.lexicon"


def test_an_unknown_nsid_never_reaches_the_database_or_a_public_artifact(
        conn, tmp_path, caplog):
    """`untracked.collection` is a schema-drift canary. A canary that names
    the thing it saw would be the unbounded-cardinality channel it exists to
    warn about — so the NSID must be absent from the counters, the persisted
    rows, the rendered page, both JSON artifacts, and the collector's log.
    """
    import logging

    from weatherwatch import report

    collector = _collector(conn)
    collector._start_run()
    with caplog.at_level(logging.INFO):
        collector._handle_raw(json.dumps(_event()))
        collector._handle_raw(json.dumps(
            _event(BASE_US + 1, collection=NOVEL_NSID)))
        collector._handle_raw(json.dumps(_event(BASE_US + WIDTH_US)))
        collector.acc.close_for_shutdown()
        collector._flush_pending()
    collector._end_run()

    persisted = {row[0] for row in conn.execute("SELECT DISTINCT metric FROM bucket")}
    assert "untracked.collection" in persisted, "the canary must have fired"
    assert NOVEL_NSID not in persisted
    assert not any(NOVEL_NSID in metric for metric in persisted)
    assert "example" not in " ".join(persisted)

    # Nowhere else in the database either — every text column of every table.
    for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            assert NOVEL_NSID not in " ".join(str(v) for v in row), table

    report.generate_report(conn, tmp_path / "site")
    for artifact in ("index.html", "summary.json", "social.json"):
        text = (tmp_path / "site" / artifact).read_text()
        assert NOVEL_NSID not in text, f"{artifact} names the unknown NSID"
        assert "novel.lexicon" not in text

    assert NOVEL_NSID not in caplog.text, "the NSID reached the log"


def test_the_canary_stays_bounded_under_many_distinct_unknown_nsids(conn):
    """Cardinality is the property, not merely absence of one name: a hundred
    novel lexicons must produce one key, not a hundred."""
    collector = _collector(conn)
    collector._handle_raw(json.dumps(_event()))
    for index in range(100):
        collector._handle_raw(json.dumps(_event(
            BASE_US + 1 + index, collection=f"com.example.lex{index:03d}")))
    window = _close_first(collector)

    assert window.counts == {"post.create": 1, "untracked.collection": 100}
    assert len(window.counts) == 2
    _assert_only(window, "unclassified", value=100)
