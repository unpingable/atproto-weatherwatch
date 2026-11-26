"""Connection liveness under replay backlog.

Production livelocked for 33 h from 2026-11-24 15:32Z: a resume at the cursor
replayed a burst minute faster than the collector could process it, the
websockets receive queue filled, reading paused, and the keepalive pong sat
unread behind the backlog. The client declared a ping timeout, discarded the
open window, resumed from the same cursor, and repeated.

These tests scale the timing down: processing is slowed, and any client
keepalive is shrunk to the same proportion it had against the real backlog.
A busy connection must survive; a silent one must still be abandoned.
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets

from weatherwatch import collector as collector_mod
from weatherwatch.collector import Collector

WIDTH = 60
BASE_US = 1_700_000_040 * 1_000_000
FLOOD = 1000
PER_FRAME_S = 0.002  # 1000 frames -> ~2 s of processing


def _frame(i: int) -> str:
    return json.dumps({
        "did": "did:example:synth0000000000000001",
        "time_us": BASE_US + i * 1_000,
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


def _shrink_client_keepalive(monkeypatch):
    """Any client keepalive is scaled to test time (production: 30 s + 10 s
    against tens of seconds of backlog). Disabled keepalive stays disabled."""
    real_connect = collector_mod.websockets.connect

    def connect(url, **kw):
        if kw.get("ping_interval", 20) is not None:
            kw["ping_interval"] = 0.2
            kw["ping_timeout"] = 0.2
        return real_connect(url, **kw)

    monkeypatch.setattr(collector_mod.websockets, "connect", connect)


def _production_wait_for(monkeypatch):
    """Python 3.10 (production) runs the awaitable as a task, so every
    `wait_for(ws.recv())` yields to the loop and the keepalive timer fires on
    schedule. 3.12 awaits inline and starves it, hiding the failure."""
    real = asyncio.wait_for

    async def wait_for(aw, timeout):
        return await real(asyncio.ensure_future(aw), timeout)

    monkeypatch.setattr(collector_mod.asyncio, "wait_for", wait_for)


def _slow_processing(monkeypatch):
    real = Collector._handle_raw

    def slow(self, raw):
        time.sleep(PER_FRAME_S)  # CPU-bound stand-in: blocks the loop
        return real(self, raw)

    monkeypatch.setattr(Collector, "_handle_raw", slow)


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/subscribe"


def test_replay_backlog_does_not_trip_liveness(conn, monkeypatch, caplog):
    _shrink_client_keepalive(monkeypatch)
    _production_wait_for(monkeypatch)
    _slow_processing(monkeypatch)
    monkeypatch.setattr(collector_mod, "FRAME_STALL_TIMEOUT_S", 0.5,
                        raising=False)
    connections = 0
    release = asyncio.Event()

    async def handler(ws, path=None):
        nonlocal connections
        connections += 1
        for i in range(FLOOD):  # the whole backlog, as fast as TCP takes it
            await ws.send(_frame(i))
        i = FLOOD
        while not release.is_set():  # then the live tail, as Jetstream does
            await ws.send(_frame(i))
            i += 1
            await asyncio.sleep(0.1)

    async def run():
        server, url = await _serve(handler)
        try:
            c = Collector(conn=conn, endpoint=url, duration_s=3.5,
                          bucket_width=WIDTH)
            await c.run()
            return c
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    c = asyncio.run(run())
    dropped = [r.getMessage() for r in caplog.records
               if r.getMessage().startswith("connection error")]
    assert dropped == [], "a busy connection was abandoned mid-backlog"
    assert c._reconnects == 0
    assert connections == 1
    assert c._messages > FLOOD


def test_silent_connection_is_abandoned(conn, monkeypatch):
    monkeypatch.setattr(collector_mod, "FRAME_STALL_TIMEOUT_S", 0.5,
                        raising=False)
    monkeypatch.setattr(collector_mod, "RECONNECT_BACKOFF_START_S", 0.05)
    connections = 0
    release = asyncio.Event()

    async def handler(ws, path=None):
        nonlocal connections
        connections += 1
        await ws.send(_frame(connections))
        await release.wait()  # alive, answers pings, sends nothing

    async def run():
        server, url = await _serve(handler)
        try:
            c = Collector(conn=conn, endpoint=url, duration_s=2.0,
                          bucket_width=WIDTH)
            await c.run()
            return c
        finally:
            release.set()
            server.close()
            await server.wait_closed()

    c = asyncio.run(run())
    assert connections >= 2, "a silent stream must be abandoned and resumed"
    assert c._reconnects >= 1
