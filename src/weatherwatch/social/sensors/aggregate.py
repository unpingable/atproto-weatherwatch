"""Tier 1 — episodes from the buckets weatherwatch already has.

Zero new retention. This sensor reads `bucket` / `window_health` through the
existing health-conditioned read layer, so it inherits, for free, every rule
that layer already enforces: unobserved windows are None and never zero,
rates divide by observed duration rather than nominal width, partial/gapped/
degraded/warming windows never teach a baseline, and runs from different
endpoints refuse to combine.

What it adds is segmentation. The deployed weather page shows mean rates and
sparklines; it does not say "these 14 consecutive minutes were a departure,
here is where it began, peaked and ended." An episode is that statement.

WHAT THIS TIER CANNOT DO, AND WHY
---------------------------------
Concentration, overlap and synchronisation are unavailable here at any effort.
A bucket is a count; the actor and target were discarded before it was
written, by design. Those detectors live in `edge.py` and need the opt-in
store. Saying so in the docstring rather than approximating it in code is
deliberate: a "concentration" number derived from counts alone would be a
fabrication with a plausible shape.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ... import derive, query, timeutil
from ..envelope import receipt_hash
from ..scope import AnalysisConfig, EvidenceSet, Finding, Scope, magnitude

DETECTOR_ID = "aggregate_rate_episode"
DETECTOR_VERSION = "v1"

#: metric -> episode type. Anything unlisted falls back to a generic name
#: rather than being dropped: an unnamed burst is still an observation.
EPISODE_TYPES: dict[str, tuple[str, str]] = {
    #  metric            (burst type,        lull type)
    "block.create":    ("block_burst",     "block_lull"),
    "block.delete":    ("unblock_burst",   "unblock_lull"),
    "like.create":     ("like_storm",      "like_lull"),
    "like.delete":     ("unlike_burst",    "unlike_lull"),
    "repost.create":   ("repost_storm",    "repost_lull"),
    "repost.delete":   ("unrepost_burst",  "unrepost_lull"),
    "follow.create":   ("follow_burst",    "follow_lull"),
    "follow.delete":   ("unfollow_burst",  "unfollow_lull"),
    "listitem.create": ("listadd_burst",   "listadd_lull"),
    "listitem.delete": ("listremove_burst", "listremove_lull"),
    "post.delete":     ("delete_storm",    "delete_lull"),
    "account.active.false": ("account_inactive_burst", "account_inactive_lull"),
}


def episode_types(metric: str) -> tuple[str, str]:
    return EPISODE_TYPES.get(metric, ("rate_burst", "rate_lull"))


#: Median-absolute-deviation to standard-deviation scale factor for a normal
#: distribution. Makes robust z comparable to an ordinary z.
MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class AggregateConfig(AnalysisConfig):
    """Interpretation parameters. Nothing here may affect selection."""

    #: "robust" = median/MAD, "mean" = mean/stddev (weatherwatch's own).
    #:
    #: Robust is the default because a trailing *mean* baseline is contaminated
    #: by the very excursion it just measured: the burst enters the baseline,
    #: the baseline rises, and a second burst minutes later scores below
    #: threshold and vanishes. That is a well-known failure class in burst
    #: detection, not a hypothetical -- `test_two_separated_bursts_do_not_merge`
    #: fails against the mean baseline and passes against this one. A median
    #: over 15 windows is unmoved by three outliers.
    #:
    #: The mean path is kept because it is exactly what the weather lane's
    #: `derive.rolling_departures` computes, and being able to reproduce that
    #: number is worth more than tidiness.
    baseline: str = "robust"
    baseline_n: int = derive.DEFAULT_BASELINE_N
    min_baseline: int = derive.MIN_BASELINE_SAMPLES
    #: z at which a window joins an episode.
    z_enter: float = 3.0
    #: z a window must still hold to keep an open episode alive.
    z_continue: float = 1.5
    #: consecutive sub-`z_continue` windows tolerated inside one episode.
    max_gap_windows: int = 2
    #: an episode must be at least this many windows and peak at least this high.
    min_windows: int = 2
    z_peak: float = 3.0
    #: emit symmetric deficit episodes. Withdrawal is an event; an instrument
    #: that only reports surges cannot see a network go quiet.
    detect_lulls: bool = True


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    return ys[mid] if n % 2 else (ys[mid - 1] + ys[mid]) / 2.0


def robust_departures(
    s: query.Series, n: int, min_samples: int,
) -> list[derive.Departure]:
    """`derive.rolling_departures` with a median/MAD baseline.

    Same conditioning rules, same output type, same "only eligible windows
    teach the baseline" discipline -- only the location and scale estimators
    differ. Falls back to stddev when MAD is zero (a perfectly flat baseline
    has no scale to speak of), and reports z=None rather than infinity when
    neither estimator has anything to say.
    """
    out: list[derive.Departure] = []
    history: list[float] = []

    for p in s.points:
        value = p.rate
        baseline = history[-n:]
        med = _median(baseline) if len(baseline) >= min_samples else None
        scale = None
        if med is not None:
            mad = _median([abs(v - med) for v in baseline])
            if mad and mad > 0:
                scale = mad * MAD_TO_SIGMA
            else:
                scale = derive.stddev(baseline)
        z = derive.zscore(value, med, scale) if value is not None else None
        pc = derive.pct_change(value, med) if value is not None else None

        out.append(derive.Departure(
            bucket_start=p.bucket_start, value=value, baseline_mean=med,
            baseline_std=scale, baseline_n=len(baseline), z=z, pct_change=pc,
            condition=derive.condition(z), eligible=p.baseline_eligible,
            quality=p.quality,
        ))
        if p.baseline_eligible and value is not None:
            history.append(value)
    return out


def _bucket_width(conn: sqlite3.Connection, run_ids: list[str]) -> int:
    """Nominal window width of the runs in scope, in seconds."""
    if not run_ids:
        return 60
    marks = ",".join("?" * len(run_ids))
    rows = conn.execute(
        f"SELECT DISTINCT bucket_width FROM observation_run "
        f"WHERE run_id IN ({marks})", run_ids).fetchall()
    return min((r[0] for r in rows), default=60)


def _to_bucket_bounds(
    conn: sqlite3.Connection, run_ids: list[str],
    since_us: int | None, until_us: int | None,
) -> tuple[int | None, int | None]:
    """Microseconds -> aligned bucket seconds. The one clock seam in the lane.

    Two units meet here and neither is wrong. The edge store timestamps events
    in Jetstream microseconds; `bucket_start` is unix *seconds*, and
    `query.series` steps its densified range by `width` from the bound it was
    given. So a microsecond bound is not merely a large number -- it is off the
    end of the table -- and an unaligned second bound is worse, because every
    step lands between buckets and the series densifies to all-unobserved
    while looking perfectly well-formed.

    Caught on real data: seven days of history returned zero episodes in
    0.13s. `test_bounds_are_converted_and_aligned` is the regression.
    """
    width = _bucket_width(conn, run_ids)
    since_s = (timeutil.bucket_start_for(since_us, width)
               if since_us is not None else None)
    # `until_us` is exclusive and `series()` steps `while b < hi`, so the last
    # bucket wanted is the one containing (until_us - 1). Using `until_us`
    # itself appends a phantom trailing window whenever the bound lands
    # exactly on a boundary, which is the common case.
    until_s = (timeutil.bucket_start_for(until_us - 1, width) + width
               if until_us is not None else None)
    return since_s, until_s


def _window_receipt(p: query.WindowPoint, metric: str) -> str:
    return receipt_hash({
        "run_id": p.run_id, "bucket_start": p.bucket_start,
        "bucket_width": p.bucket_width, "metric": metric, "count": p.count,
        "observed_duration_us": p.observed_duration_us,
        "coverage_state": p.coverage_state,
    })


def select(
    conn: sqlite3.Connection,
    run_ids: list[str],
    metric: str,
    since_us: int | None = None,
    until_us: int | None = None,
    endpoint: str = "",
) -> EvidenceSet:
    """Every window of one metric in one interval. No thresholds. No config.

    Returns the whole interval, including the windows that will support no
    finding at all — that is what makes the receipt a defence against
    cherry-picking rather than a decoration on the conclusion.
    """
    since_s, until_s = _to_bucket_bounds(conn, run_ids, since_us, until_us)
    s = query.series(conn, run_ids, metric, since=since_s, until=until_s)
    pts = s.points
    ts_start = timeutil.us_to_iso(pts[0].bucket_start * 1_000_000) if pts else ""
    ts_end = (
        timeutil.us_to_iso(
            (pts[-1].bucket_start + pts[-1].bucket_width) * 1_000_000)
        if pts else ""
    )
    scope = Scope(
        kind="aggregate",
        subject_class=metric,
        ts_start=ts_start,
        ts_end=ts_end,
        window=f"{s.bucket_width}s",
        source=endpoint or (s.endpoint or ""),
    )
    receipts = tuple(_window_receipt(p, metric) for p in pts)
    facts = {
        "n_windows": len(pts),
        "n_observed": len(s.observed_points),
        "n_eligible": len(s.eligible_points),
        "n_unobserved": sum(1 for p in pts if not p.observed),
        "total_count": s.total,
        "observed_seconds": round(s.observed_seconds, 3),
        "run_ids": list(run_ids),
    }
    return EvidenceSet(scope=scope, receipts=receipts, facts=facts, payload=(s,))


def _segments(
    deps: list[derive.Departure], cfg: AggregateConfig, sign: int,
) -> list[list[int]]:
    """Contiguous stretches of departure, by index into `deps`.

    A stretch survives `max_gap_windows` of sub-threshold weather; anything
    longer ends the episode. Two bursts separated by a quiet stretch are two
    episodes, never one — see `test_boundaries.py`.
    """
    out: list[list[int]] = []
    cur: list[int] = []
    slack = 0
    for i, d in enumerate(deps):
        z = d.z if d.z is not None else None
        entering = z is not None and sign * z >= cfg.z_enter and d.eligible
        holding = z is not None and sign * z >= cfg.z_continue and d.eligible
        if entering or (cur and holding):
            cur.append(i)
            slack = 0
        elif cur:
            slack += 1
            if slack > cfg.max_gap_windows:
                out.append(cur)
                cur, slack = [], 0
            else:
                cur.append(i)
    if cur:
        out.append(cur)
    # Trim trailing slack windows that never held.
    trimmed = []
    for seg in out:
        while seg:
            d = deps[seg[-1]]
            if d.z is None or sign * d.z < cfg.z_continue:
                seg = seg[:-1]
            else:
                break
        if seg:
            trimmed.append(seg)
    return trimmed


def _shape(zs: list[float]) -> dict:
    """Temporal shape of an episode: where the peak sits inside it."""
    if not zs:
        return {}
    peak = max(range(len(zs)), key=lambda i: zs[i])
    return {
        "rise_windows": peak,
        "fall_windows": len(zs) - peak - 1,
        "peak_position": round(peak / max(len(zs) - 1, 1), 3),
    }


def interpret(
    evidence: EvidenceSet, cfg: AggregateConfig | None = None,
) -> list[Finding]:
    """Segment the selected interval into episodes. Reads only `evidence`."""
    cfg = cfg or AggregateConfig()
    if not evidence.payload:
        return []
    s: query.Series = evidence.payload[0]
    if not s.points:
        return []

    if cfg.baseline == "mean":
        deps = derive.rolling_departures(
            s, n=cfg.baseline_n, min_samples=cfg.min_baseline)
    else:
        deps = robust_departures(
            s, n=cfg.baseline_n, min_samples=cfg.min_baseline)
    burst_type, lull_type = episode_types(evidence.scope.subject_class)

    findings: list[Finding] = []
    directions = [(1, burst_type)]
    if cfg.detect_lulls:
        directions.append((-1, lull_type))

    for sign, etype in directions:
        for seg in _segments(deps, cfg, sign):
            if len(seg) < cfg.min_windows:
                continue
            zs = [sign * deps[i].z for i in seg if deps[i].z is not None]
            if not zs or max(zs) < cfg.z_peak:
                continue

            first, last = seg[0], seg[-1]
            pts = s.points
            counts = [pts[i].count for i in seg if pts[i].count is not None]
            rates = [deps[i].value for i in seg if deps[i].value is not None]
            bmeans = [deps[i].baseline_mean for i in seg
                      if deps[i].baseline_mean is not None]
            qualities = sorted({deps[i].quality for i in seg})

            # Magnitude is the size of the departure; z decided whether there
            # was one at all. Peak rate against the episode's own baseline.
            # The extreme of a deficit is its trough, not its peak. Taking
            # max() in both directions silently reported every lull as
            # magnitude 0 -- caught on the real database, where ten of
            # forty-one episodes landed in the bottom band for this reason.
            extreme = (max(rates) if sign > 0 else min(rates)) if rates else None
            base_rate = (sum(bmeans) / len(bmeans)) if bmeans else None
            if extreme is not None and base_rate:
                ratio = (extreme / base_rate) if sign > 0 else (
                    base_rate / extreme if extreme > 0 else float("inf"))
            else:
                ratio = 1.0
            if ratio == float("inf"):
                # A collapse to exactly zero has no finite ratio. Bound it by
                # the observed span rather than reporting infinity.
                ratio = max(base_rate * 60.0, 2.0) if base_rate else 2.0

            explain = {
                "metric": evidence.scope.subject_class,
                "rate_ratio": round(ratio, 4),
                "n_windows": len(seg),
                "events_in_episode": sum(counts),
                "peak_z": round(max(zs), 3),
                "mean_z": round(sum(zs) / len(zs), 3),
                "extreme_rate_eps": round(extreme, 4)
                                    if extreme is not None else None,
                "baseline_rate_eps": round(sum(bmeans) / len(bmeans), 4)
                                     if bmeans else None,
                "baseline_estimator": cfg.baseline,
                "direction": "excess" if sign > 0 else "deficit",
                "window_quality": qualities,
                "scope_n_windows": evidence.facts.get("n_windows"),
                "scope_n_unobserved": evidence.facts.get("n_unobserved"),
            }
            explain.update(_shape(zs))

            findings.append(Finding(
                type=etype,
                ts_start=timeutil.us_to_iso(
                    pts[first].bucket_start * 1_000_000),
                ts_end=timeutil.us_to_iso(
                    (pts[last].bucket_start + pts[last].bucket_width) * 1_000_000),
                score=magnitude(ratio),
                explain=explain,
                segment_receipts=tuple(evidence.receipts[i] for i in seg),
            ))

    findings.sort(key=lambda f: (f.ts_start, f.type))
    return findings
