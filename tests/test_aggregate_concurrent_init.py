"""Focused qualification for aggregate-store initialization under a writer."""
from __future__ import annotations

from weatherwatch import db


def test_existing_aggregate_init_does_not_contend_with_active_writer(tmp_path):
    path = tmp_path / "weather.sqlite"
    owner = db.connect(path)
    db.init_db(owner)
    owner.execute("BEGIN IMMEDIATE")
    observer = db.connect(path)
    observer.execute("PRAGMA busy_timeout=50")
    try:
        # Detector and field oneshots call init_db before reading the aggregate
        # store. A current schema requires no repeated metadata mutation.
        db.init_db(observer)
    finally:
        owner.execute("ROLLBACK")
        observer.close()
        owner.close()
