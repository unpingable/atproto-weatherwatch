"""Detection runs: select -> interpret -> seal -> persist.

One function per tier, all returning sealed `DetectionEnvelope`s. The only
thing worth noticing here is what is *not* done: episodes of different types
are never merged. Two bursts on different metrics in the same minute stay two
episodes. Their coincidence is reported separately, by `co_occurrence()`, as
a presentational overlay that carries no score and asserts no relationship —
because merging them would be the first step in inventing a narrative the
records do not contain.
"""

from __future__ import annotations

import sqlite3

from .. import timeutil
from .envelope import DetectionEnvelope
from .scope import AnalysisConfig, EvidenceSet, seal
from .sensors import aggregate, edge, lifecycle
from .store import save_episode


def _watermark(ev: EvidenceSet) -> dict:
    n = ev.facts.get("n_windows") or 0
    eligible = ev.facts.get("n_eligible") or 0
    if not n:
        return {"coverage_pct": 0, "health_state": "unknown"}
    pct = round(100.0 * eligible / n, 2)
    state = "clean" if pct >= 95 else ("degraded" if pct >= 60 else "sparse")
    return {"coverage_pct": pct, "health_state": state}


def _seal_all(detector_id, detector_version, ev, findings, cfg, watermark):
    return [
        (seal(detector_id, detector_version, ev, f, cfg, watermark), ev.evidence_id)
        for f in findings
    ]


def run_aggregate(
    conn: sqlite3.Connection,
    run_ids: list[str],
    metrics: list[str],
    since_us: int | None,
    until_us: int | None,
    cfg: aggregate.AggregateConfig | None = None,
    endpoint: str = "",
) -> list[tuple[DetectionEnvelope, str]]:
    cfg = cfg or aggregate.AggregateConfig()
    out = []
    for metric in metrics:
        ev = aggregate.select(conn, run_ids, metric, since_us, until_us, endpoint)
        findings = aggregate.interpret(ev, cfg)
        out.extend(_seal_all(
            aggregate.DETECTOR_ID, aggregate.DETECTOR_VERSION,
            ev, findings, cfg, _watermark(ev)))
    return out


def run_edge(
    conn: sqlite3.Connection,
    collections: list[str],
    since_us: int,
    until_us: int,
    cfg: edge.EdgeConfig | None = None,
    op: str = "create",
) -> list[tuple[DetectionEnvelope, str]]:
    cfg = cfg or edge.EdgeConfig()
    out = []
    for collection in collections:
        ev = edge.select(conn, collection, since_us, until_us, op)
        findings = edge.interpret(ev, cfg)
        out.extend(_seal_all(
            edge.DETECTOR_ID, edge.DETECTOR_VERSION, ev, findings, cfg, {}))
    return out


def run_lifecycle(
    conn: sqlite3.Connection,
    since_us: int,
    until_us: int,
    cfg: lifecycle.LifecycleConfig | None = None,
    lookback_s: int = lifecycle.DEFAULT_LOOKBACK_S,
) -> list[tuple[DetectionEnvelope, str]]:
    cfg = cfg or lifecycle.LifecycleConfig()
    ev = lifecycle.select(conn, since_us, until_us, lookback_s=lookback_s)
    findings = lifecycle.interpret(ev, cfg)
    return _seal_all(
        lifecycle.DETECTOR_ID, lifecycle.DETECTOR_VERSION, ev, findings, cfg, {})


def persist(
    conn: sqlite3.Connection, sealed: list[tuple[DetectionEnvelope, str]],
) -> int:
    now = timeutil.now_iso()
    conn.execute("BEGIN")
    try:
        for env, evidence_id in sealed:
            save_episode(conn, env, evidence_id, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(sealed)


def co_occurrence(
    envelopes: list[DetectionEnvelope], min_overlap_s: float = 1.0,
) -> list[dict]:
    """Episodes of *different* types whose intervals overlap.

    Presentational only. No score, no envelope, no merge. Two things happening
    at once is a thing worth showing a reader and is not, on its own, a
    relationship between them.
    """
    items = []
    for e in envelopes:
        a = timeutil.to_epoch(e.ts_start)
        b = timeutil.to_epoch(e.ts_end)
        if a is None or b is None:
            continue
        items.append((a, b, e))
    items.sort(key=lambda x: x[0])

    pairs = []
    for i, (a0, a1, ea) in enumerate(items):
        for b0, b1, eb in items[i + 1:]:
            if b0 > a1:
                break
            if ea.type == eb.type:
                continue
            overlap = min(a1, b1) - max(a0, b0)
            if overlap >= min_overlap_s:
                pairs.append({
                    "a_det_id": ea.det_id, "a_type": ea.type,
                    "b_det_id": eb.det_id, "b_type": eb.type,
                    "overlap_s": round(overlap, 3),
                    "start": timeutil.us_to_iso(int(max(a0, b0) * 1_000_000)),
                })
    return pairs
