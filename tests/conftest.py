"""Shared fixtures. Every test gets its own SQLite file; nothing is shared."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weatherwatch import db

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite")
    db.init_db(c)
    yield c
    c.close()


def _load(name: str) -> list[dict]:
    path = FIXTURE_DIR / name
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


@pytest.fixture(scope="session")
def live_fixtures() -> list[dict]:
    """307 scrubbed shapes captured from a live 10-minute M0 survey."""
    return _load("jetstream_shapes.jsonl")


@pytest.fixture(scope="session")
def synthetic_fixtures() -> list[dict]:
    """Hand-written malformed / negative / scar fixtures."""
    return _load("jetstream_synthetic.jsonl")


@pytest.fixture(scope="session")
def all_fixtures(live_fixtures, synthetic_fixtures) -> list[dict]:
    return live_fixtures + synthetic_fixtures


@pytest.fixture(scope="session")
def malformed_lines() -> list[str]:
    path = FIXTURE_DIR / "malformed_lines.txt"
    return [
        ln for ln in path.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


# --- synthetic observation builder (M5–M7 tests) ---------------------------

SYNTH_BASE = 1_700_000_040  # a real 60s bucket boundary
SYNTH_WIDTH = 60
SYNTH_ENDPOINT = "wss://relay-a.invalid/subscribe"


def build_window(
    conn,
    run_id: str,
    index: int,
    metrics: dict[str, int],
    *,
    events_seen: int | None = None,
    observed_us: int | None = None,
    coverage_state: str = "ok",
    gate_reasons: str | None = None,
    gap_us: int = 0,
    resume_seam: int = 0,
    parse_errors: int = 0,
    rejected: int = 0,
    late: int = 0,
    partial: int | None = None,
    lag_ewma_s: float = 0.01,
    lag_max_s: float = 0.02,
    base: int = SYNTH_BASE,
    width: int = SYNTH_WIDTH,
):
    """Write one fully-controlled window. Used to build deterministic
    observations that would take hours to produce from a live stream."""
    start = base + index * width
    full = width * 1_000_000
    if observed_us is None:
        observed_us = full - gap_us
    if partial is None:
        partial = 1 if observed_us < full else 0
    if events_seen is None:
        events_seen = sum(metrics.values())
    for metric, count in metrics.items():
        conn.execute(
            "INSERT INTO bucket(run_id, bucket_start, bucket_width, metric, count)"
            " VALUES (?,?,?,?,?)", (run_id, start, width, metric, count))
    conn.execute(
        "INSERT INTO window_health VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, start, width, events_seen, parse_errors, 0, rejected, late,
         lag_ewma_s, lag_max_s, 0, gap_us, resume_seam,
         start * 1_000_000, start * 1_000_000 + observed_us, observed_us,
         coverage_state, gate_reasons, partial),
    )
    return start


def build_run(
    conn,
    run_id: str,
    windows: list[dict],
    *,
    endpoint: str = SYNTH_ENDPOINT,
    base: int = SYNTH_BASE,
    start_index: int = 0,
    stop_reason: str = "duration_reached",
    started_at: str = "2026-01-01T00:00:00+00:00",
    ended_at: str | None = "2026-01-01T01:00:00+00:00",
    resume_cursor: int | None = None,
):
    """Create an observation run plus its windows. `windows` entries are
    kwargs for build_window; an entry of None means that window is UNOBSERVED
    (no row written at all)."""
    db.start_run(conn, run_id, endpoint, "test", SYNTH_WIDTH, started_at,
                 None, resume_cursor)
    first = last = None
    for i, spec in enumerate(windows):
        if spec is None:
            continue  # unobserved: deliberately no row
        start = build_window(conn, run_id, start_index + i, base=base, **spec)
        first = first if first is not None else start * 1_000_000
        last = (start + SYNTH_WIDTH) * 1_000_000
    if ended_at:
        db.end_run(conn, run_id, ended_at, stop_reason, first, last)
    return run_id


def by_shape(fixtures: list[dict], shape: str) -> dict:
    """First fixture whose `_shape` matches exactly."""
    for f in fixtures:
        if f.get("_shape") == shape:
            return f["event"]
    raise AssertionError(f"no fixture with shape {shape!r}")
