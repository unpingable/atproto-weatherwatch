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


def by_shape(fixtures: list[dict], shape: str) -> dict:
    """First fixture whose `_shape` matches exactly."""
    for f in fixtures:
        if f.get("_shape") == shape:
            return f["event"]
    raise AssertionError(f"no fixture with shape {shape!r}")
