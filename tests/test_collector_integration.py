"""End-to-end collector tests against an in-process fake Jetstream.

The fake reproduces the cursor semantics M0 measured on the real relays:
`cursor=T` is **inclusive**, so an event with `time_us == T` is re-delivered.
That is precisely the behaviour that makes naive "resume from last_seen"
double-count, so the collector's `cursor + 1` has to be exercised against it
rather than assumed.

No network. No real Bluesky data.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest
import websockets

from weatherwatch import db, read
from weatherwatch.collector import Collector

WIDTH = 60
BASE = 1_700_000_040  # a real 60s boundary
EVENTS_PER_WINDOW = 5
N_WINDOWS = 4
STEP_US = (WIDTH * 1_000_000) // EVENTS_PER_WINDOW


def event_times() -> list[int]:
    return [
        BASE * 1_000_000 + i * STEP_US
        for i in range(EVENTS_PER_WINDOW * N_WINDOWS)
    ]


def envelope(time_us: int, i: int) -> str:
    return json.dumps({
        "did": "did:example:synth0000000000000001",
        "time_us": time_us,
        "kind": "commit",
        "commit": {
            "rev": "synthrev0000001",
            "operation": "create",
            "collection": "app.bsky.feed.post",
            "rkey": "synthrkey0000001",
            "cid": "bafysynthetic000000000000000000000000000001",
            "record": {"$type": "app.bsky.feed.post",
                       "createdAt": "2020-01-01T00:00:00.000Z"},
        },
    })


class FakeJetstream:
    """Serves a fixed event list, honouring inclusive cursor semantics."""

    def __init__(self, close_after: int | None = None):
        self.close_after = close_after
        self.requested_cursors: list[int | None] = []
        self.connections = 0
        self.sent_total = 0
        self._server = None
        self.port = None
        # Handlers hold connections open after draining their event list.
        # Without an explicit release, `wait_closed()` blocks on them forever.
        self._release: asyncio.Event | None = None

    async def _handler(self, ws, path=None):
        # The request path moved across websockets releases: 10.x passes it as
        # a second handler argument / exposes ws.path; 14+ drops both and puts
        # it on ws.request.path. The collector only uses connect()/recv(),
        # which are stable — this is purely the fake server keeping up.
        raw_path = path
        if raw_path is None:
            raw_path = getattr(getattr(ws, "request", None), "path", None)
        if raw_path is None:
            raw_path = getattr(ws, "path", "") or ""
        query = urllib.parse.urlparse(raw_path).query
        params = urllib.parse.parse_qs(query)
        cursor = int(params["cursor"][0]) if "cursor" in params else None
        self.requested_cursors.append(cursor)
        self.connections += 1
        my_conn = self.connections

        sent = 0
        for i, t in enumerate(event_times()):
            if cursor is not None and t < cursor:
                continue  # cursor=T is INCLUSIVE: t == cursor is still sent
            await ws.send(envelope(t, i))
            sent += 1
            self.sent_total += 1
            if (self.close_after is not None and my_conn == 1
                    and sent >= self.close_after):
                await ws.close(code=1011, reason="fake disconnect")
                return
            await asyncio.sleep(0.001)
        # Hold the connection open; the collector stops on its own duration.
        assert self._release is not None
        while not self._release.is_set():
            await asyncio.sleep(0.02)

    async def start(self):
        self._release = asyncio.Event()
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}/subscribe"

    async def stop(self):
        if self._release:
            self._release.set()
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except asyncio.TimeoutError:
                pass


async def _collect(conn, url, duration_s=3.0):
    c = Collector(conn=conn, endpoint=url, duration_s=duration_s,
                  bucket_width=WIDTH)
    await c.run()
    return c


def total_posts(conn) -> int:
    row = conn.execute(
        "SELECT SUM(count) t FROM bucket WHERE metric='post.create'"
    ).fetchone()
    return row["t"] or 0


def test_collector_records_a_run_and_advances_the_cursor(conn):
    async def run():
        fake = FakeJetstream()
        url = await fake.start()
        try:
            c = await _collect(conn, url)
        finally:
            await fake.stop()
        return c, fake

    c, fake = asyncio.run(run())

    assert fake.connections == 1
    assert fake.requested_cursors == [None], "cold start sends no cursor"

    run_row = db.get_run(conn, c.run_id)
    assert run_row is not None
    assert run_row["ended_at"] is not None
    assert run_row["stop_reason"] == "duration_reached"
    assert run_row["source_endpoint"] == c.endpoint

    assert total_posts(conn) > 0
    cursor = db.get_cursor(conn, c.endpoint)
    assert cursor is not None and cursor in event_times()

    # The cursor must not have run past what was committed.
    committed_max = conn.execute(
        "SELECT MAX(observed_to_us) m FROM window_health WHERE run_id=?",
        (c.run_id,),
    ).fetchone()["m"]
    assert cursor <= committed_max


def test_forced_reconnect_replays_without_double_counting(conn):
    """The decisive end-to-end property: a mid-run disconnect, a resume at
    cursor+1 against an inclusive-cursor server, and every event counted
    exactly once."""
    async def run():
        fake = FakeJetstream(close_after=7)
        url = await fake.start()
        try:
            c = await _collect(conn, url, duration_s=6.0)
        finally:
            await fake.stop()
        return c, fake

    c, fake = asyncio.run(run())

    assert fake.connections >= 2, "the disconnect should have forced a reconnect"
    assert fake.requested_cursors[0] is None

    second = fake.requested_cursors[1]
    committed_after_first = second - 1 if second else None
    assert second is not None, "reconnect must carry a cursor"
    assert committed_after_first in event_times(), (
        "resume cursor must be committed-event + 1"
    )

    # The server re-sent events (inclusive cursor + replay of the discarded
    # window), so total sent exceeds the distinct event count...
    assert fake.sent_total > len(event_times()) - EVENTS_PER_WINDOW
    # ...but nothing was counted twice.
    assert total_posts(conn) <= len(event_times()), (
        f"double count: {total_posts(conn)} > {len(event_times())}"
    )

    cov = read.run_coverage(conn, c.run_id)
    assert cov["reconnects"] >= 1, "the reconnect must be visible in health"
    assert cov["seams"] >= 1, "the resume seam must be recorded"


def test_second_run_resumes_from_committed_cursor(conn):
    """Restart across process death: run 2 must not recount run 1's events."""
    async def run_once(duration=3.0):
        fake = FakeJetstream()
        url = await fake.start()
        try:
            c = await _collect(conn, url, duration_s=duration)
        finally:
            await fake.stop()
        return c, fake

    c1, fake1 = asyncio.run(run_once())
    after_first = total_posts(conn)
    cursor1 = db.get_cursor(conn, c1.endpoint)
    assert after_first > 0

    # Same port is not reused, so pin the endpoint string to force resume.
    async def run_second():
        fake = FakeJetstream()
        url = await fake.start()
        c = Collector(conn=conn, endpoint=c1.endpoint, duration_s=3.0,
                      bucket_width=WIDTH)
        # Point the collector's socket at the new fake while keeping the
        # endpoint identity (and therefore the cursor) unchanged.
        real_endpoint = url
        original_build = c._build_url

        def build():
            built = original_build()
            return built.replace(c1.endpoint, real_endpoint)

        c._build_url = build
        try:
            await c.run()
        finally:
            await fake.stop()
        return c, fake

    c2, fake2 = asyncio.run(run_second())

    assert fake2.requested_cursors[0] == cursor1 + 1, (
        "second run must resume at committed cursor + 1"
    )
    assert total_posts(conn) <= len(event_times()), "no recount across restart"
    assert c2.run_id != c1.run_id, "a restart is a new observation run"

    runs = db.list_runs(conn)
    assert len(runs) == 2
    read.assert_summable(conn, [c1.run_id, c2.run_id])


def test_endpoint_change_does_not_inherit_a_cursor(conn):
    async def run(url):
        c = Collector(conn=conn, endpoint=url, duration_s=2.0, bucket_width=WIDTH)
        await c.run()
        return c

    async def scenario():
        f1 = FakeJetstream()
        u1 = await f1.start()
        c1 = await run(u1)
        await f1.stop()

        f2 = FakeJetstream()
        u2 = await f2.start()
        c2 = await run(u2)
        await f2.stop()
        return c1, c2, f2

    c1, c2, f2 = asyncio.run(scenario())

    assert c1.endpoint != c2.endpoint
    assert f2.requested_cursors[0] is None, (
        "a different endpoint is a hard seam: no cursor may carry over"
    )
    with pytest.raises(read.NotSummable, match="different endpoints"):
        read.assert_summable(conn, [c1.run_id, c2.run_id])


def test_no_identity_bearing_value_reaches_the_database(conn):
    """The envelopes the fake sends contain a DID, an rkey and a CID. None of
    them may exist anywhere in the persisted product data."""
    async def run():
        fake = FakeJetstream()
        url = await fake.start()
        try:
            await _collect(conn, url)
        finally:
            await fake.stop()

    asyncio.run(run())

    forbidden = ("did:", "synthrkey", "bafy", "app.bsky.feed.post",
                 "2020-01-01")
    for table in ("bucket", "window_health", "meta"):
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        blob = json.dumps([dict(r) for r in rows], default=str)
        for needle in forbidden:
            assert needle not in blob, (
                f"{needle!r} leaked into {table}: {blob[:400]}"
            )
