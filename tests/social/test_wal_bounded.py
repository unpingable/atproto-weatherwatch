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
