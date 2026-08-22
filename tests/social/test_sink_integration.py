"""The fork point: one parsed message, two lanes, no interference."""

from __future__ import annotations

import json

from weatherwatch import query, timeutil
from weatherwatch.collector import Collector
from weatherwatch.social import store
from weatherwatch.social.sink import SocialSink

from .conftest import BASE_US

ENDPOINT = "wss://relay-test.invalid/subscribe"


def _msgs(n=30):
    out = []
    for i in range(n):
        out.append(json.dumps({
            "did": f"did:plc:actor{i:03d}", "time_us": BASE_US + i * 1_000_000,
            "kind": "commit",
            "commit": {"rev": "r1", "operation": "create",
                       "collection": "app.bsky.graph.block",
                       "rkey": f"3k{i}", "cid": f"bafy{i}",
                       "record": {"subject": "did:plc:target",
                                  "createdAt": "2026-01-01T00:00:00Z"}},
        }))
        out.append(json.dumps({
            "did": f"did:plc:poster{i:03d}", "time_us": BASE_US + i * 1_000_000,
            "kind": "commit",
            "commit": {"rev": "r1", "operation": "create",
                       "collection": "app.bsky.feed.post",
                       "rkey": f"3p{i}", "cid": f"bafyp{i}",
                       "record": {"text": "a post nobody should retain"}},
        }))
    return out


def _drive(conn, sink, msgs):
    c = Collector(conn=conn, endpoint=ENDPOINT, social_sink=sink)
    c._start_run()
    for raw in msgs:
        c._handle_raw(raw)
    c.acc.close_for_shutdown()
    c._flush_pending()
    if sink is not None:
        sink.flush(timeutil.now_us())
    c._end_run()
    return c


def test_both_lanes_fill_from_one_parse(conn, tmp_path):
    sink = SocialSink.open(tmp_path / "social.sqlite", run_id="run-x")
    c = _drive(conn, sink, _msgs())

    s = query.series(conn, [c.run_id], "block.create")
    assert s.total == 30
    p = query.series(conn, [c.run_id], "post.create")
    assert p.total == 30

    n = sink.conn.execute("SELECT COUNT(*) FROM edge_event").fetchone()[0]
    assert n == 30, "blocks retained"
    cols = sink.conn.execute(
        "SELECT DISTINCT collection FROM edge_event").fetchall()
    assert [r[0] for r in cols] == ["block"], "posts are not retained"
    sink.conn.close()


def test_post_text_never_reaches_either_database(conn, tmp_path):
    sink = SocialSink.open(tmp_path / "social.sqlite", run_id="run-x")
    _drive(conn, sink, _msgs())
    sink.conn.close()

    for path in (tmp_path / "social.sqlite",):
        blob = path.read_bytes()
        assert b"a post nobody should retain" not in blob
    weather_blob = b"".join(
        json.dumps(dict(r)).encode()
        for r in conn.execute("SELECT * FROM bucket"))
    assert b"did:plc:" not in weather_blob
    assert b"a post nobody" not in weather_blob


def test_collections_filter_narrows_retention(conn, tmp_path):
    sink = SocialSink.open(tmp_path / "social.sqlite", run_id="run-x",
                           collections=frozenset({"follow"}))
    _drive(conn, sink, _msgs())
    n = sink.conn.execute("SELECT COUNT(*) FROM edge_event").fetchone()[0]
    assert n == 0
    snap = sink.health_snapshot()
    assert snap["skips"]["untracked_collection"] > 0
    sink.conn.close()


def test_weather_lane_survives_a_hostile_sink(conn):
    """A sensor bug must not cost stream time."""

    class Exploding:
        def observe(self, msg):
            raise RuntimeError("sensor bug")

        def maybe_flush(self, now_us):
            raise RuntimeError("sensor bug")

        def close(self, now_us):
            raise RuntimeError("sensor bug")

    c = Collector(conn=conn, endpoint=ENDPOINT, social_sink=Exploding())
    c._start_run()
    for raw in _msgs(5):
        c._handle_raw(raw)
    c.acc.close_for_shutdown()
    try:
        c._flush_pending()
    except RuntimeError:
        raise AssertionError("sink exception reached the weather flush path")
    s = query.series(conn, [c.run_id], "block.create")
    assert s.total == 5


def test_sink_without_collector_is_the_default(conn):
    c = Collector(conn=conn, endpoint=ENDPOINT)
    assert c.social_sink is None


def test_retention_horizon_prunes_on_flush(tmp_path):
    conn = store.connect(tmp_path / "social.sqlite")
    store.init_db(conn)
    from .conftest import edge as mk
    w = store.EdgeWriter(conn, "run-x", batch_rows=10_000,
                         retention_us=10 * 1_000_000)
    w.add_edge(mk("did:plc:a", "did:plc:t", BASE_US))
    w.add_edge(mk("did:plc:a", "did:plc:t2", BASE_US + 60 * 1_000_000))
    w.flush(BASE_US + 60 * 1_000_000)
    remaining = conn.execute("SELECT COUNT(*) FROM edge_event").fetchone()[0]
    assert remaining == 1, "the older edge is past the horizon"
    conn.close()
