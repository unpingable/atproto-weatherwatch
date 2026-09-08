"""Focused qualification for read-compatible initialization."""
from __future__ import annotations

from weatherwatch.social import store


def test_existing_store_init_does_not_contend_with_active_writer(tmp_path):
    path = tmp_path / "social.sqlite"
    owner = store.connect(path)
    store.init_db(owner)
    owner.execute("BEGIN IMMEDIATE")
    observer = store.connect(path)
    observer.execute("PRAGMA busy_timeout=50")
    try:
        # Existing, current schema metadata needs no content mutation. This is
        # the startup path used by the hourly detector before its short episode
        # writes, and must remain readable while the collector is flushing.
        store.init_db(observer)
    finally:
        owner.execute("ROLLBACK")
        observer.close()
        owner.close()
