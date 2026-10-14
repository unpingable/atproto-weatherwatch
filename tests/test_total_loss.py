"""Total state loss: what Weatherwatch may and may not claim afterwards.

The observatory may lose its history. It must not claim continuity or coverage
it cannot substantiate, and it must not silently shrink what it has already
published (see test_publish_continuity.py). Each test here loses the store
(a new SQLite file in a temp dir) and checks one of those claims. Local only:
the fake Jetstream binds 127.0.0.1 on an ephemeral port.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
from pathlib import Path

from weatherwatch import db, report, visibility
from weatherwatch.collector import Collector
from weatherwatch.health import ObservationHealth
from tests.conftest import SYNTH_BASE, SYNTH_ENDPOINT, build_run
from tests.test_collector_integration import (WIDTH, FakeJetstream,
                                              event_times)

NOW = dt.datetime.fromtimestamp(SYNTH_BASE + 70, tz=dt.timezone.utc)


def _store(path: Path):
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _concerns(db_path: Path, tmp_path: Path) -> dict:
    document = visibility.build_status(db_path, tmp_path / "no-report", now=NOW)
    return document, {c["id"]: c["observation"] for c in document["concerns"]}


def _collect(conn, endpoint: str, *, real_url: str, duration_s=2.0, **kw):
    """Run the real collector, pointing its socket at the fake while keeping
    the endpoint identity (and so its cursor) fixed."""
    c = Collector(conn=conn, endpoint=endpoint, duration_s=duration_s,
                  bucket_width=WIDTH, **kw)
    original = c._build_url
    c._build_url = lambda: original().replace(endpoint, real_url)
    return c


async def _run_against_fake(conn, endpoint, **kw):
    fake = FakeJetstream()
    url = await fake.start()
    try:
        c = _collect(conn, endpoint, real_url=url, **kw)
        await c.run()
    finally:
        await fake.stop()
    return c, fake


# --- store identity and continuity -----------------------------------------

def test_store_identity_is_recorded_once_at_creation(tmp_path):
    conn = _store(tmp_path / "a.sqlite")
    ident = db.store_identity(conn)
    assert ident["store_id"].startswith("store-")
    assert ident["identity_origin"] == "created"
    assert ident["history_begins_at"] == ident["store_created_at"]
    assert ident["earlier_history"] == "unknown"
    db.init_db(conn)
    assert db.store_identity(conn)["store_id"] == ident["store_id"]


def test_total_loss_yields_new_identity_and_non_present_continuity(tmp_path):
    endpoint = SYNTH_ENDPOINT
    path = tmp_path / "data" / "weatherwatch.sqlite"
    conn = _store(path)
    first_id = db.store_identity(conn)["store_id"]

    asyncio.run(_run_against_fake(conn, endpoint))
    asyncio.run(_run_against_fake(conn, endpoint))  # resumes from its cursor
    conn.close()
    _, before = _concerns(path, tmp_path)
    assert before["weatherwatch.persistence.continuity"]["local_state"] == "PRESENT"
    assert before["weatherwatch.persistence.continuity"]["facts"]["lineage"] == "RESUMED"

    shutil.rmtree(path.parent)  # total loss
    conn = _store(path)
    asyncio.run(_run_against_fake(conn, endpoint))
    conn.close()

    document, after = _concerns(path, tmp_path)
    continuity = after["weatherwatch.persistence.continuity"]
    assert continuity["local_state"] == "UNKNOWN"
    assert continuity["facts"]["lineage"] == "COLD_START"
    assert "cold start" in continuity["reason"]
    assert continuity["facts"]["store_id"] not in (None, first_id)
    store = document["extensions"]["weatherwatch"]["store"]
    assert store["store_id"] == continuity["facts"]["store_id"]
    assert store["earlier_history"] == "unknown"
    assert "History begins here; earlier history unknown." in store["note"]
    assert f"store {store['store_id']}" in visibility.render_human(document)


def test_cold_open_run_says_no_cursor_yet(tmp_path):
    path = tmp_path / "w.sqlite"
    conn = _store(path)
    build_run(conn, "r1", [], ended_at=None)
    conn.close()
    _, concerns = _concerns(path, tmp_path)
    cursor = concerns["weatherwatch.acquisition.cursor"]
    assert cursor["reason"] == "no cursor yet (cold start)"
    assert "advancing" not in cursor["reason"]
    assert concerns["weatherwatch.persistence.continuity"]["local_state"] == "UNKNOWN"


def test_summary_json_says_where_history_begins(tmp_path):
    conn = _store(tmp_path / "w.sqlite")
    build_run(conn, "r1", [{"metrics": {"post.create": 5}} for _ in range(3)])
    report.generate_report(conn, tmp_path / "site")
    summary = json.loads((tmp_path / "site" / "summary.json").read_text())
    ident = db.store_identity(conn)
    assert summary["store"]["store_id"] == ident["store_id"]
    assert summary["store"]["history_begins_at"] == ident["store_created_at"]
    assert summary["store"]["earlier_history"] == "unknown"
    assert summary["store"]["note"].startswith(
        "History begins here; earlier history unknown.")


# --- baseline checkpoint ----------------------------------------------------

def _warm(h: ObservationHealth) -> ObservationHealth:
    from tests.test_health import warm
    return warm(h, eps=330.0)


def test_baseline_is_not_restored_onto_a_new_store(tmp_path):
    path = tmp_path / "data" / "weatherwatch.sqlite"
    checkpoint = tmp_path / "data" / "baseline_checkpoint.json"
    conn = _store(path)
    old = Collector(conn=conn, endpoint=SYNTH_ENDPOINT, checkpoint_path=checkpoint)
    _warm(old.health)
    assert old.health.write_checkpoint(checkpoint)
    # Same store, same endpoint: the warm-up baseline is legitimately reused.
    same = Collector(conn=conn, endpoint=SYNTH_ENDPOINT, checkpoint_path=checkpoint)
    assert same.health.load_checkpoint(checkpoint) is True
    conn.close()

    # The database is lost but the checkpoint beside it survives.
    for p in path.parent.glob("weatherwatch.sqlite*"):
        p.unlink()
    conn = _store(path)
    fresh = Collector(conn=conn, endpoint=SYNTH_ENDPOINT, checkpoint_path=checkpoint)
    assert fresh.health.load_checkpoint(checkpoint) is False
    snap = fresh.health.snapshot()
    assert snap["baseline_restored"] is False
    assert snap["coverage_state"] != "ok"

    other = Collector(conn=conn, endpoint="wss://relay-b.invalid/subscribe",
                      checkpoint_path=checkpoint)
    assert other.health.load_checkpoint(checkpoint) is False


# --- startup replay ---------------------------------------------------------

def test_startup_replay_from_a_cursor_is_marked_as_a_seam(tmp_path):
    """An operator-set cursor on a fresh store replays history at startup.
    The first window must not read as clean live data."""
    conn = _store(tmp_path / "w.sqlite")
    db.set_meta(conn, db.cursor_key(SYNTH_ENDPOINT), str(event_times()[0] - 1))
    c, fake = asyncio.run(_run_against_fake(conn, SYNTH_ENDPOINT,
                                            duration_s=3.0))
    assert fake.requested_cursors[0] == event_times()[0]
    rows = conn.execute(
        "SELECT bucket_start, resume_seam, reconnects FROM window_health "
        "WHERE run_id=? ORDER BY bucket_start", (c.run_id,)).fetchall()
    assert rows, "the replay should have produced windows"
    assert rows[0]["resume_seam"] == 1
    assert all(r["reconnects"] == 0 for r in rows), (
        "a startup resume is not a reconnect")
    assert db.get_run(conn, c.run_id)["resume_cursor_at_start"] is not None


def test_cold_start_first_window_is_not_a_seam(tmp_path):
    conn = _store(tmp_path / "w.sqlite")
    c, _ = asyncio.run(_run_against_fake(conn, SYNTH_ENDPOINT))
    seams = conn.execute(
        "SELECT SUM(resume_seam) s FROM window_health WHERE run_id=?",
        (c.run_id,)).fetchone()["s"]
    assert seams == 0
