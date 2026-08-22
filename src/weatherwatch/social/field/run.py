"""Wiring: counters -> field -> climatology -> observations -> candidates.

Deliberately thin. The interesting decisions live in `quantities.py` (what is
measured and what it refuses), `climatology.py` (what "normal" means and when
the history cannot support one) and `observation.py` (what a record carries).
This module only puts them in order.
"""

from __future__ import annotations

import sqlite3

from ... import query, timeutil
from ..sensors.aggregate import _to_bucket_bounds
from . import FIELD_SCHEMA_VERSION
from .climatology import Climatology, build as build_climatology, candidates
from .observation import observe
from .quantities import FIELD_METRICS, build_field


def load_field(
    conn: sqlite3.Connection,
    run_ids: list,
    since_us: int | None = None,
    until_us: int | None = None,
    max_points: int = 200_000,
) -> list:
    """Field vectors over one interval.

    Bounds arrive in Jetstream microseconds and `bucket_start` is unix
    seconds; `_to_bucket_bounds` is shared with the aggregate sensor rather
    than reimplemented, because getting that conversion wrong once already
    cost a silent zero-result (see its docstring).
    """
    since_s, until_s = _to_bucket_bounds(conn, run_ids, since_us, until_us)
    series_map = {
        m: query.series(conn, run_ids, m, since=since_s, until=until_s,
                        max_points=max_points)
        for m in FIELD_METRICS
    }
    return build_field(series_map)


def build_all(
    conn: sqlite3.Connection,
    run_ids: list,
    since_us: int | None = None,
    until_us: int | None = None,
    endpoint: str = "",
    collector_version: str = "",
) -> tuple:
    """Returns (points, climatology, observations, candidates)."""
    points = load_field(conn, run_ids, since_us, until_us)
    if not points:
        return [], None, [], []

    width = points[0].bucket_width
    provenance = {
        "endpoint": endpoint,
        "run_ids": sorted(run_ids),
        "collector_version": collector_version,
        "field_schema_version": FIELD_SCHEMA_VERSION,
        "source": "weatherwatch bucket counters (identity-free)",
    }
    clim = build_climatology(points, window=f"{width}s", provenance=provenance)
    obs = [observe(p, clim, provenance) for p in points]
    cands = candidates(points, clim)
    return points, clim, obs, cands


def replay_observation(document: dict) -> str:
    """Recompute a stored observation's id from its own document.

    The stored `document` is the canonical form, so re-deriving the id from it
    must reproduce the stored id exactly. That is what makes the archive
    replayable rather than merely persisted.
    """
    from ..envelope import receipt_hash

    # Strip ONLY the id. `structural_absences` is part of what the id commits
    # to -- the record's statement of what it cannot measure is content, not
    # presentation, and excluding it here made replay silently disagree.
    body = {k: v for k, v in document.items() if k != "observation_id"}
    return receipt_hash(body)
