"""M5 — the read side.

Boring queries over the M1–M4 schema. The whole job of this module is to make
three things impossible for a caller to get wrong:

1. **Unobserved time is not zero activity.** A window with no `window_health`
   row was never watched; its count is `None`, never `0`. A window that *was*
   watched and simply had no events of that metric has a real `0`. Those are
   different facts and this module keeps them different all the way out.

2. **Partial windows are not full windows.** Every rate divides by the
   *observed* duration recorded at collection time, not by the nominal bucket
   width.

3. **Incompatible observations do not combine.** `read.assert_summable`
   already refuses different endpoints and overlapping intervals; every entry
   point here goes through it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import read

# --- window quality vocabulary --------------------------------------------
# Flags are additive metadata. `quality` is the single worst-case label for
# display. Eligibility for baselines is computed separately, because a seam
# with successfully reconstructed data is NOT loss.

FLAG_PARTIAL = "partial"
FLAG_SEAM = "seam"
FLAG_GAP = "gap"
FLAG_DEGRADED = "degraded"
FLAG_WARMING = "warming_up"
FLAG_LOSS = "loss"
FLAG_LAGGED = "lagged"
FLAG_RECOVERING = "recovering"
FLAG_UNOBSERVED = "unobserved"

#: Worst-first. The first flag present becomes the display `quality`.
QUALITY_PRECEDENCE = (
    FLAG_UNOBSERVED, FLAG_GAP, FLAG_LOSS, FLAG_DEGRADED,
    FLAG_PARTIAL, FLAG_WARMING, FLAG_LAGGED, FLAG_RECOVERING, FLAG_SEAM,
)
QUALITY_CLEAN = "clean"

# ---------------------------------------------------------------------------
# THE CONDITIONING RULE
#
# A window's *counts* are trustworthy exactly when three directly-measured
# facts hold: it was observed for its full duration, its gap is zero, and its
# instrumented loss buckets are zero. Those are measurements of completeness.
#
# `coverage_state` is something else — a derived heuristic about the health of
# the stream, and it mixes two independent axes:
#
#   latency  — how far behind real time we are. Resuming from a persisted
#              cursor replays backlog, so a collector chewing through 50
#              minutes of history is thousands of seconds "behind" while
#              missing nothing. Recovery hysteresis then holds the state at
#              DEGRADED with no active trigger for several more windows.
#   coverage — whether we actually saw everything.
#
# Only the coverage axis may disqualify a window's counts. Latency and
# hysteresis are recorded as metadata (`lagged`, `recovering`) and stay
# baseline-eligible. This is a read-side interpretation; the collector's raw
# `coverage_state` and `gate_reasons` are preserved untouched and remain
# available on every point.
# ---------------------------------------------------------------------------

#: Gate reasons that mean "behind real time", not "missing data".
LATENCY_ONLY_REASONS = frozenset({"lag_high"})

#: Directly-measured completeness defects. Any of these disqualifies counts.
COVERAGE_DEFECT_FLAGS = frozenset({FLAG_PARTIAL, FLAG_GAP, FLAG_LOSS})

#: Run status vocabulary.
RUN_OPEN = "open"
RUN_INTERRUPTED = "interrupted"
RUN_PARTIAL = "partial"
RUN_COMPLETE = "complete"

NORMAL_STOP_REASONS = frozenset({
    "duration_reached", "completed", "signal_sigint", "signal_sigterm",
})


class QueryTooLarge(ValueError):
    """Densifying the requested span would produce an absurd number of points.

    Raised rather than silently truncating: a truncated series read as a whole
    one is exactly the class of mistake this module exists to prevent.
    """


@dataclass(frozen=True)
class WindowPoint:
    """One bucket of one metric, with everything needed to condition it."""

    bucket_start: int
    bucket_width: int
    #: None means UNOBSERVED. 0 means observed and genuinely zero.
    count: int | None
    events_seen: int | None
    observed_duration_us: int
    flags: frozenset[str]
    coverage_state: str
    run_id: str | None
    gap_us: int = 0
    lag_ewma_s: float | None = None
    lag_max_s: float | None = None

    @property
    def observed(self) -> bool:
        return FLAG_UNOBSERVED not in self.flags

    @property
    def observed_seconds(self) -> float:
        return self.observed_duration_us / 1_000_000

    @property
    def rate(self) -> float | None:
        """Events per second over the time we were ACTUALLY watching.

        Dividing by nominal width would understate a partial window's rate;
        dividing by observed duration is what makes a 13-second first window
        comparable to a full one.
        """
        if self.count is None or self.observed_duration_us <= 0:
            return None
        return self.count / self.observed_seconds

    @property
    def quality(self) -> str:
        for f in QUALITY_PRECEDENCE:
            if f in self.flags:
                return f
        return QUALITY_CLEAN

    @property
    def baseline_eligible(self) -> bool:
        """Usable as trailing-baseline input. See THE CONDITIONING RULE above.

        Excluded: unobserved, partial, gapped, lossy, coverage-degraded, and
        warming-up (where the collector cannot yet tell a quiet network from a
        shedding one).

        Included: `seam` — a reconnect whose interval was reconstructed by
        cursor+1 replay is complete data, and dropping it would punish the
        collector for recovering correctly. Also `lagged` and `recovering`,
        which describe latency, not missing events.
        """
        if not self.observed or self.count is None:
            return False
        bad = {FLAG_PARTIAL, FLAG_GAP, FLAG_DEGRADED, FLAG_WARMING, FLAG_LOSS}
        return not (self.flags & bad)


@dataclass(frozen=True)
class Series:
    metric: str
    endpoint: str
    run_ids: tuple[str, ...]
    bucket_width: int
    points: tuple[WindowPoint, ...]

    @property
    def observed_points(self) -> tuple[WindowPoint, ...]:
        return tuple(p for p in self.points if p.observed)

    @property
    def eligible_points(self) -> tuple[WindowPoint, ...]:
        return tuple(p for p in self.points if p.baseline_eligible)

    @property
    def total(self) -> int:
        return sum(p.count for p in self.points if p.count is not None)

    @property
    def observed_seconds(self) -> float:
        return sum(p.observed_seconds for p in self.points if p.observed)

    @property
    def mean_rate(self) -> float | None:
        """Total events over total observed time. Unobserved time contributes
        to neither numerator nor denominator — no interpolation."""
        secs = self.observed_seconds
        return (self.total / secs) if secs > 0 else None


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    endpoint: str
    collector_version: str
    bucket_width: int
    started_at: str
    ended_at: str | None
    planned_end: str | None
    stop_reason: str | None
    resume_cursor_at_start: int | None
    first_event_us: int | None
    last_event_us: int | None
    windows: int
    partial_windows: int
    empty_windows: int
    degraded_windows: int
    lagged_windows: int
    seam_windows: int
    gap_windows: int
    events: int
    observed_duration_us: int
    nominal_duration_us: int
    gap_us: int
    reconnects: int
    parse_errors: int
    unclassified: int
    rejected_no_time_us: int
    late_events: int
    lag_ewma_max_s: float | None
    lag_max_s: float | None
    status: str

    @property
    def wall_duration_s(self) -> float | None:
        """Wall-clock length of the session."""
        from . import timeutil
        if not self.ended_at:
            return None
        a = timeutil.to_epoch(self.started_at)
        b = timeutil.to_epoch(self.ended_at)
        return (b - a) if (a and b) else None

    @property
    def data_span_s(self) -> float | None:
        """Length of the STREAM interval the data covers.

        Not the same as wall duration: a run resuming from an old cursor
        replays history, so it can cover far more stream time than it spent
        running. Reporting wall duration as the data interval would be wrong.
        """
        if self.first_event_us is None or self.last_event_us is None:
            return None
        return (self.last_event_us - self.first_event_us) / 1_000_000

    @property
    def coverage_ratio(self) -> float | None:
        if not self.nominal_duration_us:
            return None
        return self.observed_duration_us / self.nominal_duration_us

    @property
    def replayed(self) -> bool:
        return self.resume_cursor_at_start is not None


# --- flag computation ------------------------------------------------------

def _flags_for(row: sqlite3.Row) -> frozenset[str]:
    """Translate a stored health row into conditioning flags.

    Implements THE CONDITIONING RULE documented at the top of this module:
    directly-measured completeness defects (partial / gap / loss) disqualify a
    window's counts; latency and recovery hysteresis do not.
    """
    flags: set[str] = set()
    if row["partial"]:
        flags.add(FLAG_PARTIAL)
    if row["resume_seam"]:
        flags.add(FLAG_SEAM)
    if (row["gap_us"] or 0) > 0:
        flags.add(FLAG_GAP)

    loss = ((row["parse_errors"] or 0) + (row["rejected_no_time_us"] or 0)
            + (row["late_events"] or 0))
    if loss > 0:
        flags.add(FLAG_LOSS)

    state = row["coverage_state"]
    reasons = {r for r in (row["gate_reasons"] or "").split(",") if r}
    if state == "degraded":
        coverage_affecting = (reasons - LATENCY_ONLY_REASONS) or (
            flags & COVERAGE_DEFECT_FLAGS)
        if coverage_affecting:
            flags.add(FLAG_DEGRADED)
        elif reasons:
            flags.add(FLAG_LAGGED)       # lag_high only: complete but late
        else:
            flags.add(FLAG_RECOVERING)   # hysteresis, no active trigger
    elif state == "warming_up":
        flags.add(FLAG_WARMING)
    return frozenset(flags)


# --- runs ------------------------------------------------------------------

def _run_status(row: sqlite3.Row, agg: sqlite3.Row) -> str:
    if row["ended_at"] is None:
        return RUN_OPEN
    if row["stop_reason"] not in NORMAL_STOP_REASONS:
        return RUN_INTERRUPTED
    if (agg["gap_windows"] or 0) or (agg["degraded_windows"] or 0):
        return RUN_PARTIAL
    return RUN_COMPLETE


_AGG_SQL = """
SELECT COUNT(*) AS windows,
       COALESCE(SUM(partial), 0) AS partial_windows,
       COALESCE(SUM(events_seen = 0), 0) AS empty_windows,
       COALESCE(SUM(coverage_state = 'degraded' AND NOT (
           COALESCE(gate_reasons,'') IN ('', 'lag_high')
           AND partial = 0 AND gap_us = 0 AND parse_errors = 0
           AND rejected_no_time_us = 0 AND late_events = 0)), 0)
           AS degraded_windows,
       COALESCE(SUM(coverage_state = 'degraded' AND
           COALESCE(gate_reasons,'') IN ('', 'lag_high')
           AND partial = 0 AND gap_us = 0 AND parse_errors = 0
           AND rejected_no_time_us = 0 AND late_events = 0), 0)
           AS lagged_windows,
       COALESCE(SUM(resume_seam), 0) AS seam_windows,
       COALESCE(SUM(gap_us > 0), 0) AS gap_windows,
       COALESCE(SUM(events_seen), 0) AS events,
       COALESCE(SUM(observed_duration_us), 0) AS observed_duration_us,
       COALESCE(SUM(bucket_width), 0) * 1000000 AS nominal_duration_us,
       COALESCE(SUM(gap_us), 0) AS gap_us,
       COALESCE(SUM(reconnects), 0) AS reconnects,
       COALESCE(SUM(parse_errors), 0) AS parse_errors,
       COALESCE(SUM(unclassified), 0) AS unclassified,
       COALESCE(SUM(rejected_no_time_us), 0) AS rejected_no_time_us,
       COALESCE(SUM(late_events), 0) AS late_events,
       MAX(lag_ewma_s) AS lag_ewma_max_s,
       MAX(lag_max_s) AS lag_max_s
FROM window_health WHERE run_id = ?
"""


def run_summary(conn: sqlite3.Connection, run_id: str) -> RunSummary:
    row = conn.execute(
        "SELECT * FROM observation_run WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no such run: {run_id}")
    agg = conn.execute(_AGG_SQL, (run_id,)).fetchone()
    return RunSummary(
        run_id=row["run_id"],
        endpoint=row["source_endpoint"],
        collector_version=row["collector_version"],
        bucket_width=row["bucket_width"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        planned_end=row["planned_end"],
        stop_reason=row["stop_reason"],
        resume_cursor_at_start=row["resume_cursor_at_start"],
        first_event_us=row["first_event_us"],
        last_event_us=row["last_event_us"],
        windows=agg["windows"],
        partial_windows=agg["partial_windows"],
        empty_windows=agg["empty_windows"],
        degraded_windows=agg["degraded_windows"],
        lagged_windows=agg["lagged_windows"],
        seam_windows=agg["seam_windows"],
        gap_windows=agg["gap_windows"],
        events=agg["events"],
        observed_duration_us=agg["observed_duration_us"],
        nominal_duration_us=agg["nominal_duration_us"],
        gap_us=agg["gap_us"],
        reconnects=agg["reconnects"],
        parse_errors=agg["parse_errors"],
        unclassified=agg["unclassified"],
        rejected_no_time_us=agg["rejected_no_time_us"],
        late_events=agg["late_events"],
        lag_ewma_max_s=agg["lag_ewma_max_s"],
        lag_max_s=agg["lag_max_s"],
        status=_run_status(row, agg),
    )


def list_run_summaries(conn: sqlite3.Connection, limit: int = 50) -> list[RunSummary]:
    ids = [r["run_id"] for r in conn.execute(
        "SELECT run_id FROM observation_run ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )]
    return [run_summary(conn, r) for r in ids]


def latest_run_id(conn: sqlite3.Connection, endpoint: str | None = None) -> str | None:
    if endpoint:
        row = conn.execute(
            "SELECT run_id FROM observation_run WHERE source_endpoint=? "
            "ORDER BY started_at DESC LIMIT 1", (endpoint,)).fetchone()
    else:
        row = conn.execute(
            "SELECT run_id FROM observation_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return row["run_id"] if row else None


def compatible_runs(
    conn: sqlite3.Connection, endpoint: str, limit: int = 50
) -> list[str]:
    """Most recent runs on one endpoint that can legitimately be combined.

    Walks newest-first and stops at the first run that would overlap, so the
    result is always a summable set rather than a hopeful one.
    """
    rows = conn.execute(
        "SELECT run_id FROM observation_run WHERE source_endpoint=? "
        "ORDER BY started_at DESC LIMIT ?", (endpoint, limit),
    ).fetchall()
    chosen: list[str] = []
    for r in rows:
        trial = chosen + [r["run_id"]]
        try:
            read.assert_summable(conn, trial)
        except read.NotSummable:
            break
        chosen = trial
    return list(reversed(chosen))


# --- series ----------------------------------------------------------------

_SERIES_SQL = """
SELECT h.run_id, h.bucket_start, h.bucket_width, h.events_seen,
       h.observed_duration_us, h.partial, h.resume_seam, h.gap_us,
       h.coverage_state, h.gate_reasons, h.parse_errors, h.rejected_no_time_us,
       h.late_events, h.lag_ewma_s, h.lag_max_s,
       COALESCE(b.count, 0) AS count
FROM window_health h
LEFT JOIN bucket b
       ON b.run_id = h.run_id
      AND b.bucket_start = h.bucket_start
      AND b.bucket_width = h.bucket_width
      AND b.metric = ?
WHERE h.run_id IN ({placeholders})
{time_clause}
ORDER BY h.bucket_start
"""


def series(
    conn: sqlite3.Connection,
    run_ids: list[str],
    metric: str,
    since: int | None = None,
    until: int | None = None,
    densify: bool = True,
    max_points: int = 20_000,
) -> Series:
    """Per-window counts for one metric.

    The LEFT JOIN is the load-bearing part: a `window_health` row with no
    matching `bucket` row means we watched and saw none of that metric, which
    is a real zero. A missing `window_health` row means we were not watching,
    which `densify` renders as `count=None`.
    """
    if not run_ids:
        raise ValueError("no runs selected")
    read.assert_summable(conn, run_ids)

    meta = conn.execute(
        "SELECT DISTINCT source_endpoint, bucket_width FROM observation_run "
        f"WHERE run_id IN ({','.join('?' * len(run_ids))})", run_ids,
    ).fetchall()
    if not meta:
        raise KeyError("no such run(s)")
    endpoints = {m["source_endpoint"] for m in meta}
    widths = {m["bucket_width"] for m in meta}
    if len(endpoints) != 1:
        raise read.NotSummable(f"multiple endpoints: {sorted(endpoints)}")
    if len(widths) != 1:
        raise read.NotSummable(f"mixed bucket widths: {sorted(widths)}")
    endpoint = endpoints.pop()
    width = widths.pop()

    time_clause = ""
    params: list = [metric, *run_ids]
    if since is not None:
        time_clause += " AND h.bucket_start >= ?"
        params.append(since)
    if until is not None:
        time_clause += " AND h.bucket_start < ?"
        params.append(until)

    sql = _SERIES_SQL.format(
        placeholders=",".join("?" * len(run_ids)), time_clause=time_clause
    )
    rows = conn.execute(sql, params).fetchall()

    observed: dict[int, WindowPoint] = {}
    for r in rows:
        observed[r["bucket_start"]] = WindowPoint(
            bucket_start=r["bucket_start"],
            bucket_width=r["bucket_width"],
            count=r["count"],
            events_seen=r["events_seen"],
            observed_duration_us=r["observed_duration_us"],
            flags=_flags_for(r),
            coverage_state=r["coverage_state"],
            run_id=r["run_id"],
            gap_us=r["gap_us"] or 0,
            lag_ewma_s=r["lag_ewma_s"],
            lag_max_s=r["lag_max_s"],
        )

    if not observed:
        return Series(metric, endpoint, tuple(run_ids), width, ())

    if not densify:
        pts = tuple(observed[k] for k in sorted(observed))
        return Series(metric, endpoint, tuple(run_ids), width, pts)

    lo = since if since is not None else min(observed)
    hi = until if until is not None else max(observed) + width
    n = (hi - lo) // width
    if n > max_points:
        raise QueryTooLarge(
            f"{n} windows requested (max {max_points}). Narrow the range: "
            "silently truncating would turn an incomplete series into one "
            "that looks complete."
        )

    points: list[WindowPoint] = []
    b = lo
    while b < hi:
        if b in observed:
            points.append(observed[b])
        else:
            # NOT observed. count is None, never 0.
            points.append(WindowPoint(
                bucket_start=b, bucket_width=width, count=None,
                events_seen=None, observed_duration_us=0,
                flags=frozenset({FLAG_UNOBSERVED}),
                coverage_state=FLAG_UNOBSERVED, run_id=None,
            ))
        b += width
    return Series(metric, endpoint, tuple(run_ids), width, tuple(points))


def available_metrics(conn: sqlite3.Connection, run_ids: list[str]) -> list[str]:
    ph = ",".join("?" * len(run_ids))
    return [r["metric"] for r in conn.execute(
        f"SELECT DISTINCT metric FROM bucket WHERE run_id IN ({ph}) "
        "ORDER BY metric", run_ids)]


def metric_totals(conn: sqlite3.Connection, run_ids: list[str]) -> dict[str, int]:
    read.assert_summable(conn, run_ids)
    ph = ",".join("?" * len(run_ids))
    return {r["metric"]: r["total"] for r in conn.execute(
        f"SELECT metric, SUM(count) AS total FROM bucket "
        f"WHERE run_id IN ({ph}) GROUP BY metric ORDER BY total DESC", run_ids)}


def sum_series(parts: list[Series], name: str) -> Series:
    """Add several aligned series together, read-side only.

    For presentation composites like "list mutations = listitem creates +
    listitem deletes". Nothing is persisted: the underlying keys stay the
    product data and this sum exists only for the length of a render.

    Alignment is by `bucket_start`, and a window unobserved in ANY component
    is unobserved in the sum — never a partial total dressed up as a whole
    one. Components come from the same runs and the same densification, so in
    practice their window sets are identical; the guard is for the day that
    stops being true.
    """
    if not parts:
        raise ValueError("no series to sum")
    first = parts[0]
    for p in parts[1:]:
        if p.bucket_width != first.bucket_width:
            raise ValueError("bucket widths differ")
        if p.run_ids != first.run_ids:
            raise ValueError("sum requires series over the same runs")

    maps = [{q.bucket_start: q for q in p.points} for p in parts]
    points: list[WindowPoint] = []
    for base in first.points:
        others = [m.get(base.bucket_start) for m in maps]
        if any(o is None or not o.observed for o in others) or not base.observed:
            points.append(WindowPoint(
                bucket_start=base.bucket_start, bucket_width=base.bucket_width,
                count=None, events_seen=base.events_seen,
                observed_duration_us=0,
                flags=frozenset({FLAG_UNOBSERVED}),
                coverage_state=FLAG_UNOBSERVED, run_id=base.run_id,
            ))
            continue
        total = sum(o.count or 0 for o in others)
        points.append(WindowPoint(
            bucket_start=base.bucket_start, bucket_width=base.bucket_width,
            count=total, events_seen=base.events_seen,
            observed_duration_us=base.observed_duration_us,
            flags=base.flags, coverage_state=base.coverage_state,
            run_id=base.run_id, gap_us=base.gap_us,
            lag_ewma_s=base.lag_ewma_s, lag_max_s=base.lag_max_s,
        ))
    return Series(name, first.endpoint, first.run_ids, first.bucket_width,
                  tuple(points))


TOTAL_EVENTS_METRIC = "_events_total"


def total_events_series(
    conn: sqlite3.Connection, run_ids: list[str], densify: bool = True
) -> Series:
    """All observed events per window, from `window_health.events_seen`.

    Not a `bucket` metric — it counts every message the collector saw,
    including ones that classified as unclassified. Useful as the denominator
    of "how busy was the network" without summing a metric vocabulary that
    double-counts (a reply post emits both `post.create` and
    `post.create.reply`).
    """
    base = series(conn, run_ids, TOTAL_EVENTS_METRIC, densify=densify)
    points = tuple(
        WindowPoint(
            bucket_start=p.bucket_start, bucket_width=p.bucket_width,
            count=p.events_seen, events_seen=p.events_seen,
            observed_duration_us=p.observed_duration_us, flags=p.flags,
            coverage_state=p.coverage_state, run_id=p.run_id,
            gap_us=p.gap_us, lag_ewma_s=p.lag_ewma_s, lag_max_s=p.lag_max_s,
        )
        for p in base.points
    )
    return Series(TOTAL_EVENTS_METRIC, base.endpoint, base.run_ids,
                  base.bucket_width, points)


def observation_window_health(
    conn: sqlite3.Connection, run_ids: list[str]
) -> list[WindowPoint]:
    """Health-only view: one point per observed window, metric-independent."""
    read.assert_summable(conn, run_ids)
    ph = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT *, 0 AS count FROM window_health WHERE run_id IN ({ph}) "
        "ORDER BY bucket_start", run_ids,
    ).fetchall()
    return [
        WindowPoint(
            bucket_start=r["bucket_start"], bucket_width=r["bucket_width"],
            count=r["events_seen"], events_seen=r["events_seen"],
            observed_duration_us=r["observed_duration_us"],
            flags=_flags_for(r), coverage_state=r["coverage_state"],
            run_id=r["run_id"], gap_us=r["gap_us"] or 0,
            lag_ewma_s=r["lag_ewma_s"], lag_max_s=r["lag_max_s"],
        )
        for r in rows
    ]
