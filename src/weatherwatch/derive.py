"""M6 — read-time derived weather. Nothing here is persisted.

Every formula is written out below and implemented exactly once. The rules
that keep them honest:

* **Rates divide by observed duration**, never nominal width. A 13-second
  first window and a 60-second full window are directly comparable.
* **Baselines use only eligible windows** (`WindowPoint.baseline_eligible`):
  no unobserved, partial, gapped, degraded, warming-up, or lossy windows.
  Seams stay in — a reconnect whose interval was reconstructed by cursor+1
  replay is complete data.
* **Nothing is interpolated across unobserved time.** Unobserved windows are
  skipped, not filled, not zeroed, not averaged over.
* **Denominator zero yields None, never 0 and never infinity.** A ratio
  nobody can compute is missing, not extreme.

Formulas
--------

    observed_seconds(w)   = w.observed_duration_us / 1e6
    rate(m, w)            = count(m, w) / observed_seconds(w)
    ratio(a, b, w)        = count(a, w) / count(b, w)          [None if b == 0]

    baseline(s, w, n)     = last n eligible points strictly BEFORE w
    rolling_mean          = sum(values) / n
    rolling_std           = sample stddev, ddof=1               [None if n < 2]
    zscore(x)             = (x - mean) / std                    [None if std == 0]
    pct_change(x)         = (x - mean) / mean                   [None if mean == 0]

    condition(z)          = surging   if z >= 3
                            elevated  if 1.5 <= z < 3
                            normal    if -1.5 < z < 1.5
                            quiet     if -3 < z <= -1.5
                            degrading if z <= -3
                            unknown   if z is None

`condition` labels are a convenience for reading a dashboard at a glance.
They are threshold cuts on a z-score against a short trailing baseline of the
same stream. They are not calibrated against anything, they have no
statistical warrant beyond "this hour looked different from the last few
minutes", and they must not be presented as if they did.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .query import Series, WindowPoint

#: Trailing eligible windows used for a baseline.
DEFAULT_BASELINE_N = 15
#: Below this many eligible windows the baseline is refused outright.
MIN_BASELINE_SAMPLES = 5

COND_SURGING = "surging"
COND_ELEVATED = "elevated"
COND_NORMAL = "normal"
COND_QUIET = "quiet"
COND_DEGRADING = "degrading"
COND_UNKNOWN = "unknown"

Z_SURGING = 3.0
Z_ELEVATED = 1.5
Z_QUIET = -1.5
Z_DEGRADING = -3.0


# --- primitives ------------------------------------------------------------

def rate(point: WindowPoint) -> float | None:
    """Events per observed second. None when unobserved."""
    return point.rate


def ratio(numerator: int | None, denominator: int | None) -> float | None:
    """None when either side is missing or the denominator is zero.

    Zero denominators are common and boring (no posts in a quiet window), so
    they must not produce an infinity that a chart then renders as a spike.
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def stddev(values: list[float]) -> float | None:
    """Sample standard deviation (ddof=1). None for fewer than two points."""
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    var = sum((v - m) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(var)


def zscore(x: float, m: float | None, s: float | None) -> float | None:
    if m is None or s is None or s == 0:
        return None
    return (x - m) / s


def pct_change(x: float, m: float | None) -> float | None:
    if m is None or m == 0:
        return None
    return (x - m) / m


def condition(z: float | None) -> str:
    if z is None:
        return COND_UNKNOWN
    if z >= Z_SURGING:
        return COND_SURGING
    if z >= Z_ELEVATED:
        return COND_ELEVATED
    if z <= Z_DEGRADING:
        return COND_DEGRADING
    if z <= Z_QUIET:
        return COND_QUIET
    return COND_NORMAL


# --- rolling baselines -----------------------------------------------------

@dataclass(frozen=True)
class Departure:
    """One window measured against its own trailing baseline."""

    bucket_start: int
    value: float | None
    baseline_mean: float | None
    baseline_std: float | None
    baseline_n: int
    z: float | None
    pct_change: float | None
    condition: str
    eligible: bool
    quality: str


def rolling_departures(
    s: Series,
    n: int = DEFAULT_BASELINE_N,
    min_samples: int = MIN_BASELINE_SAMPLES,
) -> list[Departure]:
    """Rate-vs-trailing-baseline for every point in a series.

    The baseline for window *w* is the last `n` **eligible** windows strictly
    before *w*. Eligible windows are collected in stream order, so an
    unobserved or degraded stretch is stepped over rather than filled — the
    baseline simply reaches further back in wall-clock time, which is the
    honest behaviour. It never invents a value for a window nobody watched.

    Every point gets a row, including unobserved ones (value None, condition
    unknown), so a caller cannot lose track of where the holes are.
    """
    out: list[Departure] = []
    history: list[float] = []

    for p in s.points:
        value = p.rate
        baseline = history[-n:]
        bmean = mean(baseline) if len(baseline) >= min_samples else None
        bstd = stddev(baseline) if len(baseline) >= min_samples else None
        z = zscore(value, bmean, bstd) if value is not None else None
        pc = pct_change(value, bmean) if value is not None else None

        out.append(Departure(
            bucket_start=p.bucket_start,
            value=value,
            baseline_mean=bmean,
            baseline_std=bstd,
            baseline_n=len(baseline),
            z=z,
            pct_change=pc,
            condition=condition(z),
            eligible=p.baseline_eligible,
            quality=p.quality,
        ))

        # Only eligible windows teach the baseline.
        if p.baseline_eligible and value is not None:
            history.append(value)

    return out


# --- ratio series ----------------------------------------------------------

@dataclass(frozen=True)
class RatioPoint:
    bucket_start: int
    numerator: int | None
    denominator: int | None
    value: float | None
    observed: bool
    quality: str


def ratio_series(a: Series, b: Series) -> list[RatioPoint]:
    """Per-window ratio of two aligned series.

    Both series must come from the same runs and bucket width, so alignment
    is by `bucket_start` rather than by position. A window unobserved in
    either series is unobserved in the ratio.
    """
    if a.bucket_width != b.bucket_width:
        raise ValueError("bucket widths differ")
    if a.run_ids != b.run_ids:
        raise ValueError("ratio requires series over the same runs")
    bmap = {p.bucket_start: p for p in b.points}

    out: list[RatioPoint] = []
    for p in a.points:
        q = bmap.get(p.bucket_start)
        if q is None or not p.observed or not q.observed:
            out.append(RatioPoint(p.bucket_start, p.count,
                                  q.count if q else None, None, False,
                                  "unobserved"))
            continue
        out.append(RatioPoint(
            bucket_start=p.bucket_start,
            numerator=p.count,
            denominator=q.count,
            value=ratio(p.count, q.count),
            observed=True,
            quality=p.quality if p.quality != "clean" else q.quality,
        ))
    return out


#: Ratios worth showing by default. Each is (label, numerator, denominator).
#: All are counts of public aggregate events; none require identity.
STANDARD_RATIOS: tuple[tuple[str, str, str], ...] = (
    ("reply/post", "post.create.reply", "post.create"),
    ("quote/post", "post.create.quote", "post.create"),
    ("repost/post", "repost.create", "post.create"),
    ("like/post", "like.create", "post.create"),
    ("block/follow", "block.create", "follow.create"),
    ("post delete/create", "post.delete", "post.create"),
    ("like delete/create", "like.delete", "like.create"),
    ("follow delete/create", "follow.delete", "follow.create"),
    ("repost delete/create", "repost.delete", "repost.create"),
)

#: Collections whose create/delete pairs make a delete/create ratio meaningful.
DELETE_CREATE_COLLECTIONS = (
    "post", "like", "repost", "follow", "block", "listitem",
)

#: Metrics counted as graph mutations. Aggregate counts of graph-mutating
#: EVENTS — deliberately not a graph, not edges, not actors.
GRAPH_MUTATION_METRICS = (
    "follow.create", "follow.delete",
    "block.create", "block.delete",
    "listitem.create", "listitem.delete",
)


def aggregate_rate(series_list: list[Series]) -> float | None:
    """Combined events/sec across several metrics over one observation.

    Sums counts and divides by the observed seconds of the FIRST series;
    all inputs are over the same runs and windows, so their observed time is
    identical by construction.
    """
    if not series_list:
        return None
    secs = series_list[0].observed_seconds
    if secs <= 0:
        return None
    return sum(s.total for s in series_list) / secs


# --- lagged correlation (small, generic, non-causal) -----------------------

def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def lagged_correlation(
    a: Series, b: Series, max_lag: int = 5, min_pairs: int = 10
) -> dict[int, float | None]:
    """Pearson r between two rate series at integer window lags.

    `lag = k` correlates a[t] against b[t + k]: positive k asks whether *a*
    tends to lead *b*. Only window pairs where BOTH sides are
    baseline-eligible are used, so unobserved and degraded stretches drop out
    rather than being filled.

    This is a descriptive primitive. **It licenses no causal claim.** Two
    metrics on one platform share every confounder there is — time of day,
    traffic volume, a single viral thread. Read it as "these moved together",
    never as "this caused that".
    """
    amap = {p.bucket_start: p for p in a.points}
    bmap = {p.bucket_start: p for p in b.points}
    width = a.bucket_width
    out: dict[int, float | None] = {}

    for lag in range(-max_lag, max_lag + 1):
        xs: list[float] = []
        ys: list[float] = []
        for start, pa in amap.items():
            pb = bmap.get(start + lag * width)
            if pb is None:
                continue
            if not (pa.baseline_eligible and pb.baseline_eligible):
                continue
            if pa.rate is None or pb.rate is None:
                continue
            xs.append(pa.rate)
            ys.append(pb.rate)
        out[lag] = pearson(xs, ys) if len(xs) >= min_pairs else None
    return out
