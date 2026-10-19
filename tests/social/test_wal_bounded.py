"""The social store's WAL must be bounded by its writers, not by luck.

Measured on the deployed host (2026-10-19): `social.sqlite-wal` sat at 1.64 GB
beside a 2.58 GB database and never reset. Nothing checkpointed explicitly,
and three timer-driven jobs plus the publisher kept a connection on the file
almost continuously, so SQLite's automatic PASSIVE checkpoint could copy
frames back but never restart the log, and nothing ever shrank it.

The contract these tests pin:

* every connection carries an explicit `wal_autocheckpoint` and a
  `journal_size_limit`, so a log that does restart is also cut back to size;
* every job's close path attempts `wal_checkpoint(TRUNCATE)` and, when a
  reader still holds a snapshot, falls back to PASSIVE without failing the job.

The "idle peer" connection in these tests is the production shape: another
process has the file open but is not mid-read. Without a peer, SQLite's own
last-connection close checkpoints and deletes the log, which would make a
missing truncation invisible to the test.
"""

from __future__ import annotations

import os
import sqlite3

from weatherwatch.social import store


def _wal(path) -> int:
    wal = f"{path}-wal"
    return os.path.getsize(wal) if os.path.exists(wal) else -1


def idle_peer(path):
    """Another holder of the file that has read from it and is now idle.

    It must have *read*: a connection that never touched the database holds
    nothing, and SQLite would delete the log on the writer's close anyway.
    """
    peer = store.connect(path)
    store.init_db(peer)
    peer.execute("SELECT COUNT(*) FROM meta").fetchone()
    return peer


def _write_some(conn, n=2_000):
    store.init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        [(f"k{i}", "x" * 512) for i in range(n)])
    conn.execute("COMMIT")


def test_connect_sets_explicit_wal_bounds(tmp_path):
    conn = store.connect(tmp_path / "social.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
                == store.WAL_AUTOCHECKPOINT_PAGES)
        assert (conn.execute("PRAGMA journal_size_limit").fetchone()[0]
                == store.JOURNAL_SIZE_LIMIT_BYTES)
        assert store.WAL_AUTOCHECKPOINT_PAGES > 0, "never disable autocheckpoint"
    finally:
        conn.close()


def test_close_truncates_the_wal_while_another_process_has_the_file_open(
        tmp_path):
    path = tmp_path / "social.sqlite"
    peer = idle_peer(path)              # open, idle: the deployed shape
    conn = store.connect(path)
    try:
        _write_some(conn)
        assert _wal(path) > 0, "fixture did not produce a WAL to bound"
        store.close(conn)
        assert _wal(path) == 0, "the job's close path left the WAL in place"
    finally:
        peer.close()


def test_checkpoint_falls_back_to_passive_while_a_reader_holds_a_snapshot(
        tmp_path):
    path = tmp_path / "social.sqlite"
    conn = store.connect(path)
    _write_some(conn, n=10)
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                             isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM meta").fetchone()
    try:
        _write_some(conn)
        result = store.checkpoint(conn, busy_timeout_ms=50)
        assert result["mode"] == "PASSIVE"
        assert result["truncated"] is False
        # The job's own busy_timeout survives the shortened checkpoint wait.
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        store.close(conn)                 # must not raise
    finally:
        reader.execute("COMMIT")
    # Once the reader has gone, the next job to finish truncates the log.
    peer = idle_peer(path)
    try:
        store.close(store.connect(path))
        assert _wal(path) == 0
    finally:
        reader.close()
        peer.close()


def test_checkpoint_is_skipped_inside_an_open_transaction(tmp_path):
    conn = store.connect(tmp_path / "social.sqlite")
    store.init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        assert store.checkpoint(conn)["mode"] == "skipped"
    finally:
        conn.execute("ROLLBACK")
        conn.close()


# --- every job that opens the store closes it through the same path --------

def _weather_db(tmp_path):
    from weatherwatch import db
    from tests.social.field.conftest import write_hourly_run

    path = tmp_path / "weather.sqlite"
    conn = db.connect(path)
    db.init_db(conn)
    write_hourly_run(conn, "run-a", n_days=10)
    conn.close()
    return path


def _range_args():
    from weatherwatch import timeutil
    from tests.social.field.conftest import BASE, HOUR

    return ["--since", timeutil.us_to_iso(BASE * 1_000_000),
            "--until", timeutil.us_to_iso((BASE + 216 * HOUR) * 1_000_000)]


def _leave_a_wal(path):
    """A writer that closed without checkpointing: the pre-fix shape."""
    conn = store.connect(path)
    _write_some(conn)
    conn.close()
    assert _wal(path) > 0


def test_field_sealing_leaves_no_wal_behind(tmp_path, capsys):
    from weatherwatch import cli

    weather = _weather_db(tmp_path)
    social = tmp_path / "social.sqlite"
    peer = idle_peer(social)
    try:
        rc = cli.main(["--db", str(weather), "social", "field", *_range_args(),
                       "--social-db", str(social)])
        assert rc == 0, capsys.readouterr().err
        assert _wal(social) == 0, "field sealing left its WAL in place"
    finally:
        peer.close()


def test_detection_leaves_no_wal_behind(tmp_path, capsys):
    from weatherwatch import cli

    weather = _weather_db(tmp_path)
    social = tmp_path / "social.sqlite"
    peer = idle_peer(social)
    try:
        _leave_a_wal(social)
        rc = cli.main(["--db", str(weather), "social", "detect",
                       "--tiers", "aggregate", *_range_args(),
                       "--social-db", str(social)])
        assert rc == 0, capsys.readouterr().err
        assert _wal(social) == 0, "detection left its WAL in place"
    finally:
        peer.close()


def test_detection_refusal_still_closes_through_the_checkpoint(tmp_path,
                                                                capsys):
    from weatherwatch import cli

    weather = _weather_db(tmp_path)
    social = tmp_path / "social.sqlite"
    peer = idle_peer(social)
    try:
        _leave_a_wal(social)
        rc = cli.main(["--db", str(weather), "social", "detect",
                       "--tiers", "edge", "--social-db", str(social)])
        assert rc == 2
        assert _wal(social) == 0, "an early return skipped the close path"
    finally:
        peer.close()


def _edges(sink, start, n=3_000):
    from weatherwatch.social.edges import EdgeEvent
    for i in range(start, start + n):
        sink.writer.add_edge(EdgeEvent(
            observed_us=1_000_000 + i, actor_did=f"did:plc:a{i}",
            collection="block", op="create", subject_kind="did",
            subject_ref="did:plc:t", rkey=f"r{i}", rev="1",
            cid=f"c{i}", record_created_at="2026-01-01T00:00:00Z"))


def test_the_collector_flush_never_truncates_and_close_does(tmp_path,
                                                            monkeypatch):
    """The flush runs on the collector's ingest path. A TRUNCATE there is a
    synchronous frame copy, fsync and truncate every minute -- and up to a
    whole field run's frames when a job's close had to settle for PASSIVE.
    The flush leaves truncation to the batch jobs; the sink's close, at
    shutdown, still truncates."""
    from weatherwatch.social import sink as sink_mod

    calls = []
    real = sink_mod.checkpoint

    def spy(conn, **kwargs):
        calls.append(kwargs)
        return real(conn, **kwargs)

    monkeypatch.setattr(sink_mod, "checkpoint", spy)
    social = tmp_path / "social.sqlite"
    sink = sink_mod.SocialSink.open(social, "run-x")
    peer = idle_peer(social)
    try:
        _edges(sink, 0)
        sink.flush(2_000_000)
        assert calls == [], "a small WAL needs no checkpoint from the collector"
        assert _wal(social) > 0

        monkeypatch.setattr(sink_mod, "FLUSH_CHECKPOINT_WAL_BYTES", 1)
        _edges(sink, 3_000)
        sink.flush(3_000_000)
        assert calls == [{"busy_timeout_ms": 0, "passive_only": True}]
        assert _wal(social) > 0, "the collector's flush truncated the WAL"

        _edges(sink, 6_000)
        sink.close(4_000_000)
        assert _wal(social) == 0, "the sink's close left the WAL in place"
    finally:
        peer.close()


def test_the_collector_threshold_matches_the_journal_size_limit():
    from weatherwatch.social import sink as sink_mod
    assert sink_mod.FLUSH_CHECKPOINT_WAL_BYTES == store.JOURNAL_SIZE_LIMIT_BYTES


def test_a_passive_only_checkpoint_does_not_truncate(tmp_path):
    path = tmp_path / "social.sqlite"
    peer = idle_peer(path)
    conn = store.connect(path)
    try:
        _write_some(conn)
        result = store.checkpoint(conn, passive_only=True)
        assert result["mode"] == "PASSIVE" and result["truncated"] is False
        assert _wal(path) > 0
    finally:
        conn.close()
        peer.close()


def test_the_report_reader_releases_and_truncates(tmp_path):
    from weatherwatch import report

    social = tmp_path / "social.sqlite"
    peer = idle_peer(social)
    try:
        _leave_a_wal(social)
        report._load_conditions(social, "2026-01-01T00:00:00Z")
        assert _wal(social) == 0
    finally:
        peer.close()
