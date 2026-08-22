"""Fixtures for the social-weather field.

Windows here are HOURLY, not minute-wide. The climatology conditions on hour
of day and counts *day* replicates, so a fixture has to span real days; at
60s that would be 1,440 rows per day and the tests would crawl. One row per
hour gives eight days in 192 windows and exercises exactly the same code
paths.
"""

from __future__ import annotations

import math
import zlib

import pytest

from weatherwatch import db

HOUR = 3600
#: An exact hour boundary, so each window is precisely one hour-of-day cell.
BASE = 1_700_000_000 - (1_700_000_000 % HOUR)
ENDPOINT = "wss://relay-field.invalid/subscribe"

FIELD_METRIC_SHAPE = {
    "post.create": 100.0,
    "post.create.reply": 40.0,
    "post.create.quote": 8.0,
    "repost.create": 60.0,
    "like.create": 400.0,
    "follow.create": 50.0,
    "follow.delete": 10.0,
    "block.create": 9.0,
    "block.delete": 1.0,
    "listitem.create": 4.0,
    "listitem.delete": 1.0,
    "post.delete": 7.0,
    "like.delete": 9.0,
    "repost.delete": 3.0,
}


def diurnal_factor(hour: int) -> float:
    """A smooth daily cycle, so hour-conditioning has something to find."""
    return 1.0 + 0.55 * math.sin((hour - 6) / 24.0 * 2 * math.pi)


def write_hourly_run(
    conn, run_id: str, n_days: int = 8, *, start_day: int = 0,
    scale: dict | None = None, unobserved: set | None = None,
    spike: dict | None = None, endpoint: str = ENDPOINT,
):
    """Create a run of hourly windows with a diurnal cycle.

    `unobserved` is a set of absolute window indices to leave with no row at
    all -- the collector was not watching, which must never read as zero.
    `spike` maps window index -> multiplier applied to every metric.
    """
    unobserved = unobserved or set()
    spike = spike or {}
    scale = scale or {}
    db.start_run(conn, run_id, endpoint, "test", HOUR,
                 "2026-01-01T00:00:00+00:00", None, None)
    first = last = None
    total = n_days * 24
    for i in range(total):
        idx = start_day * 24 + i
        if idx in unobserved:
            continue
        start = BASE + idx * HOUR
        hour = (idx) % 24
        factor = diurnal_factor(hour) * spike.get(idx, 1.0)
        counts = {}
        for metric, base_rate in FIELD_METRIC_SHAPE.items():
            per_hour = base_rate * HOUR * scale.get(metric, 1.0) * factor
            # Jitter so distributions are not degenerate. `hash()` is
            # randomised per process for str, so using it here made the whole
            # fixture change between runs and the spike test flaky. crc32 is
            # stable across processes.
            seed = zlib.crc32(metric.encode()) % 7
            jitter = 1.0 + 0.03 * math.sin(idx * 1.7 + seed)
            counts[metric] = max(int(per_hour * jitter), 0)
        events = sum(counts.values())
        for metric, count in counts.items():
            conn.execute(
                "INSERT OR REPLACE INTO bucket(run_id, bucket_start, "
                "bucket_width, metric, count) VALUES (?,?,?,?,?)",
                (run_id, start, HOUR, metric, count))
        conn.execute(
            "INSERT OR REPLACE INTO window_health VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, start, HOUR, events, 0, 0, 0, 0, 0.01, 0.02, 0, 0, 0,
             start * 1_000_000, (start + HOUR) * 1_000_000,
             HOUR * 1_000_000, "ok", None, 0))
        first = first if first is not None else start * 1_000_000
        last = (start + HOUR) * 1_000_000
    db.end_run(conn, run_id, "2026-01-09T00:00:00+00:00", "duration_reached",
               first, last)
    return run_id


@pytest.fixture()
def field_conn(tmp_path):
    c = db.connect(tmp_path / "field.sqlite")
    db.init_db(c)
    yield c
    c.close()
