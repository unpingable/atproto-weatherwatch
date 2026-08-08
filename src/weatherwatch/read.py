"""Minimal read side. Enough to inspect a database and to make the
"runs are not additive" rule mechanical. No dashboard, no HTTP, no API.

The one idea here worth stating: **summing across observation runs is a
claim, not an arithmetic convenience.** Two runs may come from different
relays (M0 measured a 1.6x volume divergence between public Jetstream
instances) or may overlap in time. Adding those together produces a number
that describes no actual observation. `assert_summable` refuses.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class NotSummable(ValueError):
    """Raised when runs cannot honestly be added together."""


@dataclass(frozen=True)
class RunSpan:
    run_id: str
    endpoint: str
    start_us: int | None
    end_us: int | None


def _run_spans(conn: sqlite3.Connection, run_ids: list[str]) -> list[RunSpan]:
    spans = []
    for rid in run_ids:
        row = conn.execute(
            "SELECT run_id, source_endpoint, first_event_us, last_event_us "
            "FROM observation_run WHERE run_id=?", (rid,)
        ).fetchone()
        if row is None:
            raise NotSummable(f"unknown run {rid!r}")
        # Fall back to window coverage when a run recorded no events.
        lo, hi = row["first_event_us"], row["last_event_us"]
        if lo is None or hi is None:
            b = conn.execute(
                "SELECT MIN(bucket_start) lo, MAX(bucket_start + bucket_width) hi "
                "FROM window_health WHERE run_id=?", (rid,)
            ).fetchone()
            if b and b["lo"] is not None:
                lo = lo if lo is not None else b["lo"] * 1_000_000
                hi = hi if hi is not None else b["hi"] * 1_000_000
        spans.append(RunSpan(row["run_id"], row["source_endpoint"], lo, hi))
    return spans


def assert_summable(conn: sqlite3.Connection, run_ids: list[str]) -> None:
    """Refuse to aggregate runs that do not describe one observation.

    Permitted: several runs from the SAME endpoint whose observed intervals
    do not overlap — e.g. a collector restarted across a reboot. Those are
    consecutive pieces of one timeline.

    Refused: different endpoints (different views of the network, not the
    same measurement), and overlapping intervals (double observation).
    """
    if len(run_ids) < 2:
        return
    spans = _run_spans(conn, run_ids)

    endpoints = {s.endpoint for s in spans}
    if len(endpoints) > 1:
        raise NotSummable(
            "refusing to sum runs from different endpoints "
            f"{sorted(endpoints)}: these are different observations of the "
            "network, not one observation. M0 measured ~1.6x volume "
            "divergence between public relays."
        )

    dated = sorted(
        [s for s in spans if s.start_us is not None and s.end_us is not None],
        key=lambda s: s.start_us,
    )
    for a, b in zip(dated, dated[1:]):
        if b.start_us < a.end_us:
            raise NotSummable(
                f"refusing to sum overlapping runs {a.run_id} and {b.run_id}: "
                "their observed intervals intersect, so adding them would "
                "count the same stream twice"
            )


def metric_series(
    conn: sqlite3.Connection,
    run_ids: list[str],
    metric: str,
) -> list[sqlite3.Row]:
    """Per-window counts for one metric across explicitly named runs."""
    assert_summable(conn, run_ids)
    placeholders = ",".join("?" * len(run_ids))
    return conn.execute(
        f"SELECT b.bucket_start, b.bucket_width, SUM(b.count) AS count, "
        f"       MIN(h.observed_duration_us) AS observed_duration_us, "
        f"       MAX(h.partial) AS partial, "
        f"       MAX(h.coverage_state = 'degraded') AS degraded, "
        f"       MAX(h.resume_seam) AS resume_seam "
        f"FROM bucket b "
        f"JOIN window_health h ON h.run_id = b.run_id "
        f"  AND h.bucket_start = b.bucket_start "
        f"  AND h.bucket_width = b.bucket_width "
        f"WHERE b.run_id IN ({placeholders}) AND b.metric = ? "
        f"GROUP BY b.bucket_start, b.bucket_width ORDER BY b.bucket_start",
        (*run_ids, metric),
    ).fetchall()


def run_totals(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT metric, SUM(count) AS total FROM bucket WHERE run_id=? "
        "GROUP BY metric ORDER BY total DESC",
        (run_id,),
    ).fetchall()


def run_coverage(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    """Coverage summary. `observed_duration_us` is time we were actually
    watching — nominal window count times width would overstate it whenever
    a window was partial.
    """
    return conn.execute(
        "SELECT COUNT(*) AS windows, "
        "       SUM(partial) AS partial_windows, "
        "       SUM(events_seen) AS events, "
        "       SUM(observed_duration_us) AS observed_duration_us, "
        "       SUM(bucket_width) * 1000000 AS nominal_duration_us, "
        "       SUM(gap_us) AS gap_us, "
        "       SUM(reconnects) AS reconnects, "
        "       SUM(resume_seam) AS seams, "
        "       SUM(parse_errors) AS parse_errors, "
        "       SUM(unclassified) AS unclassified, "
        "       SUM(rejected_no_time_us) AS rejected, "
        "       SUM(late_events) AS late_events, "
        "       SUM(CASE WHEN events_seen = 0 THEN 1 ELSE 0 END) AS empty_windows "
        "FROM window_health WHERE run_id=?",
        (run_id,),
    ).fetchone()
