"""What is the normal weather? Answered before anything is called unusual.

This module exists because "unusual" is otherwise defined by whoever picked
the threshold. A departure means nothing without a denominator, and the
denominator has to be honest about its own thinness.

TWO KINDS OF SAMPLE SIZE
------------------------
A fortnight of minute windows looks like 20,000 samples. It is not. Adjacent
minutes are strongly autocorrelated -- the same measurement already ran on
this instrument's own data and found lag-1 r of 0.83 for `block.create`,
giving an effective N of 157 rather than 1,687. So every distribution here
reports **n_eff** alongside n, using the AR(1) correction
`n_eff = n * (1 - r) / (1 + r)`.

The second kind matters more for conditioning. To ask "is this unusual *for
this hour of day*", the independent replicates are **days**, not windows: 14
days of data give ~14 replicates per hour-of-day cell however many minutes
each contains. To ask "unusual for this hour of the week" the replicates are
**weeks**, and a fortnight gives two. That is why `hour_of_week` is computed,
reported, and marked unsupported rather than quietly used.

Nothing here is a detector. `candidates()` marks windows that sit outside
their own hour-conditioned spread and says so in exactly those words.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import asdict, dataclass, field

from ..envelope import receipt_hash
from . import FIELD_SCHEMA_VERSION
from .quantities import QUANTITY_BY_NAME, QUANTITY_NAMES, FieldPoint

SUPPORTED = "supported"
THIN = "thin"
UNSUPPORTED = "unsupported"

#: Independent-replicate floors for conditioning on hour of day.
#:
#: An hour cell's 5th and 95th percentiles are estimated from one sample per
#: day. At 8 days the "5th percentile" is essentially the minimum of eight
#: numbers and moves wholesale when any one of them does; tails need more
#: replicates than quartiles do. Hence 20 for supported rather than a week.
MIN_DAYS_SUPPORTED = 20
MIN_DAYS_THIN = 7
#: Effective-sample floors, applied to the DESEASONALISED residual.
MIN_NEFF_SUPPORTED = 100.0
MIN_NEFF_THIN = 30.0
#: Weeks of history before hour-of-week conditioning is worth using.
MIN_WEEKS_FOR_HOUR_OF_WEEK = 8
#: An hour cell whose 5th-95th spread is below this fraction of its own level
#: is treated as having no usable spread. Without it, a quantity that barely
#: moves marks every window as unusual.
DEGENERATE_SPREAD_FRACTION = 0.01
#: A value must clear the band by this fraction of the band's own width before
#: it counts as outside it. Observed without it: a 6x spike flagged
#: `interaction_pressure` as *below* its 5th percentile by 0.0001, because the
#: spike scaled numerator and denominator alike and the cell's band was
#: 0.07 wide. Being outside a range by a rounding error is not being outside
#: it, and unmargined comparisons fill the candidate list with arithmetic.
MIN_EXCEEDANCE_FRACTION = 0.05


def percentile(xs: list, q: float) -> float | None:
    """Linear-interpolated percentile. `xs` need not be sorted."""
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = (len(ys) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ys[int(pos)]
    return ys[lo] + (ys[hi] - ys[lo]) * (pos - lo)


def lag1_autocorrelation(xs: list) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    den = sum((x - m) ** 2 for x in xs)
    if den <= 0:
        return None
    num = sum((xs[i] - m) * (xs[i + 1] - m) for i in range(n - 1))
    return num / den


def effective_n(n: int, r: float | None) -> float | None:
    """AR(1) effective sample size, clamped to [1, n].

    Both bounds are real. Below one is nonsense. *Above n* is also nonsense,
    and the formula produces it whenever the series is negatively
    autocorrelated: `(1-r)/(1+r)` exceeds 1 for r < 0, so an alternating
    series reports more independent samples than it has observations.

    Observed on the live estate: `acceleration` (lag-1 r = -0.37, n = 17,273)
    reported n_eff = 37,576. Anti-correlation genuinely does carry more
    information per observation than independence does, but not more
    information than there are observations, and a baseline report claiming
    otherwise undermines the one number it exists to be honest about.
    """
    if r is None or n <= 0:
        return None
    r = max(min(r, 0.999), -0.999)
    return max(1.0, min(float(n), n * (1.0 - r) / (1.0 + r)))


@dataclass(frozen=True)
class Distribution:
    n: int
    n_eff: float | None
    lag1_r: float | None
    mean: float | None
    sd: float | None
    p05: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p95: float | None
    minimum: float | None
    maximum: float | None
    #: Autocorrelation of the *deseasonalised* residual, and the effective
    #: sample size derived from it. The raw series is dominated by the daily
    #: cycle -- a smooth diurnal swing alone pushes lag-1 r above 0.95 and
    #: drives n_eff to single digits -- but conditioning happens inside an
    #: hour cell, so the residual is the series whose independence actually
    #: governs the baseline. Both are reported; support keys on the residual.
    lag1_r_residual: float | None = None
    n_eff_residual: float | None = None


def distribution(xs: list, residuals: list | None = None) -> Distribution:
    vals = [x for x in xs if x is not None]
    if not vals:
        return Distribution(
            n=0, n_eff=None, lag1_r=None, mean=None, sd=None, p05=None,
            p25=None, p50=None, p75=None, p95=None, minimum=None,
            maximum=None)
    n = len(vals)
    mean = sum(vals) / n
    sd = (math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
          if n > 1 else None)
    r = lag1_autocorrelation(vals)
    res = [x for x in (residuals or []) if x is not None]
    rr = lag1_autocorrelation(res) if len(res) >= 3 else None
    return Distribution(
        n=n, n_eff=effective_n(n, r), lag1_r=r,
        lag1_r_residual=rr,
        n_eff_residual=effective_n(len(res), rr) if rr is not None else None,
        mean=mean, sd=sd,
        p05=percentile(vals, 0.05), p25=percentile(vals, 0.25),
        p50=percentile(vals, 0.50), p75=percentile(vals, 0.75),
        p95=percentile(vals, 0.95),
        minimum=min(vals), maximum=max(vals),
    )


@dataclass(frozen=True)
class DiurnalCell:
    hour: int
    n: int
    n_days: int          # independent replicates, the number that matters
    p05: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p95: float | None


@dataclass(frozen=True)
class QuantityClimatology:
    quantity: str
    unit: str
    summary: str
    non_claim: str
    overall: Distribution
    diurnal: tuple
    support: str
    support_note: str


@dataclass(frozen=True)
class Climatology:
    schema_version: int
    window: str
    ts_start: str
    ts_end: str
    n_windows: int
    n_observed: int
    n_eligible: int
    n_days: int
    n_weeks: int
    hour_of_week_supported: bool
    hour_of_week_note: str
    quantities: dict
    provenance: dict = field(default_factory=dict)

    @property
    def climatology_id(self) -> str:
        return receipt_hash(self.as_dict(include_id=False))

    def as_dict(self, include_id: bool = True) -> dict:
        d = {
            "schema_version": self.schema_version,
            "window": self.window,
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
            "n_windows": self.n_windows,
            "n_observed": self.n_observed,
            "n_eligible": self.n_eligible,
            "n_days": self.n_days,
            "n_weeks": self.n_weeks,
            "hour_of_week_supported": self.hour_of_week_supported,
            "hour_of_week_note": self.hour_of_week_note,
            "quantities": {
                k: {
                    "quantity": v.quantity, "unit": v.unit,
                    "summary": v.summary, "non_claim": v.non_claim,
                    "overall": asdict(v.overall),
                    "diurnal": [asdict(c) for c in v.diurnal],
                    "support": v.support, "support_note": v.support_note,
                }
                for k, v in sorted(self.quantities.items())
            },
            "provenance": self.provenance,
        }
        if include_id:
            d["climatology_id"] = self.climatology_id
        return d


def _hour_of(bucket_start: int) -> int:
    return datetime.datetime.fromtimestamp(
        bucket_start, tz=datetime.timezone.utc).hour


def _date_of(bucket_start: int) -> str:
    return datetime.datetime.fromtimestamp(
        bucket_start, tz=datetime.timezone.utc).date().isoformat()


def _support_for(dist: Distribution, n_days: int) -> tuple:
    if dist.n == 0:
        return UNSUPPORTED, "no eligible windows carried this quantity."
    neff = dist.n_eff_residual or dist.n_eff or 0.0
    raw = f"{dist.lag1_r:.2f}" if dist.lag1_r is not None else "n/a"
    res = (f"{dist.lag1_r_residual:.2f}"
           if dist.lag1_r_residual is not None else "n/a")
    stats = (f"{n_days} day replicates per hour cell; n={dist.n} windows, "
             f"n_eff={neff:.0f} on the deseasonalised residual "
             f"(lag-1 r {res}; raw {raw}).")
    if n_days >= MIN_DAYS_SUPPORTED and neff >= MIN_NEFF_SUPPORTED:
        return SUPPORTED, stats
    if n_days >= MIN_DAYS_THIN and neff >= MIN_NEFF_THIN:
        return THIN, (
            stats + f" Below the {MIN_DAYS_SUPPORTED}-day floor for stable "
            "5th/95th estimates: usable for description, too thin to carry "
            "confident expectations.")
    return UNSUPPORTED, (
        stats + " Reported so the gap is visible, not because it can carry a "
        "claim.")


def build(
    points: list, window: str, provenance: dict | None = None,
) -> Climatology:
    """Climatology over eligible windows only. Never fills a hole."""
    eligible = [p for p in points if p.eligible]
    observed = [p for p in points if p.observed]
    days = sorted({_date_of(p.bucket_start) for p in eligible})
    weeks = sorted({
        datetime.datetime.fromtimestamp(
            p.bucket_start, tz=datetime.timezone.utc).isocalendar()[:2]
        for p in eligible
    })

    # Group by hour once. Rescanning every point for each of 24 hours for
    # each of 9 quantities is 24*n*9 passes -- ~39M at a fortnight of minute
    # windows, which is minutes of pointless work.
    by_hour: dict = {h: [] for h in range(24)}
    for p in eligible:
        by_hour[_hour_of(p.bucket_start)].append(p)
    days_by_hour = {
        h: len({_date_of(p.bucket_start) for p in ps})
        for h, ps in by_hour.items()
    }

    quantities: dict = {}
    for name in QUANTITY_NAMES:
        q = QUANTITY_BY_NAME[name]
        vals = [p.values.get(name) for p in eligible]

        # Cells first: the residual is defined against them.
        cells = []
        medians: dict = {}
        for hour in range(24):
            in_hour = by_hour[hour]
            hv = [p.values.get(name) for p in in_hour
                  if p.values.get(name) is not None]
            med = percentile(hv, 0.50)
            medians[hour] = med
            cells.append(DiurnalCell(
                hour=hour, n=len(hv),
                n_days=days_by_hour[hour],
                p05=percentile(hv, 0.05), p25=percentile(hv, 0.25),
                p50=med, p75=percentile(hv, 0.75),
                p95=percentile(hv, 0.95),
            ))

        # Deseasonalise against the hour cell each window belongs to, so the
        # reported independence is that of the series the baseline uses.
        residuals = []
        for p in eligible:
            v = p.values.get(name)
            med = medians.get(_hour_of(p.bucket_start))
            residuals.append(None if v is None or med is None else v - med)

        dist = distribution(vals, residuals)
        support, note = _support_for(dist, len(days))
        quantities[name] = QuantityClimatology(
            quantity=name, unit=q.unit, summary=q.summary,
            non_claim=q.non_claim, overall=dist, diurnal=tuple(cells),
            support=support, support_note=note,
        )

    how_supported = len(weeks) >= MIN_WEEKS_FOR_HOUR_OF_WEEK
    return Climatology(
        schema_version=FIELD_SCHEMA_VERSION,
        window=window,
        ts_start=_iso(min(p.bucket_start for p in points)) if points else "",
        ts_end=(_iso(max(p.bucket_start + p.bucket_width for p in points))
                if points else ""),
        n_windows=len(points), n_observed=len(observed),
        n_eligible=len(eligible), n_days=len(days), n_weeks=len(weeks),
        hour_of_week_supported=how_supported,
        hour_of_week_note=(
            f"{len(weeks)} distinct weeks observed. Conditioning on hour of "
            f"week needs independent *weeks*, not windows: 168 cells against "
            f"{len(weeks)} replicates each is not a baseline, so it is not "
            f"computed. Hour of day is conditioned on {len(days)} day "
            f"replicates and is."
        ),
        quantities=quantities,
        provenance=provenance or {},
    )


def _iso(epoch_s: int) -> str:
    return datetime.datetime.fromtimestamp(
        epoch_s, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Candidate:
    """A window outside its own hour-conditioned spread. Not a finding."""

    bucket_start: int
    quantity: str
    value: float
    direction: str        # "above" | "below"
    cell_p05: float | None
    cell_p95: float | None
    cell_n_days: int
    support: str
    note: str = (
        "Candidate only: this window sits outside the 5th-95th percentile of "
        "its own hour-of-day cell. It is not a finding, not an incident, and "
        "not attributable to anyone."
    )


def candidates(
    points: list, clim: Climatology, quantities: list | None = None,
) -> list:
    """Windows outside their hour cell's p05-p95. Explicitly not detections.

    Skips any quantity whose climatology is `unsupported`: marking a window
    unusual against a baseline that cannot carry the claim is how a threshold
    becomes a fact.
    """
    names = quantities or list(QUANTITY_NAMES)
    out: list = []
    for name in names:
        qc = clim.quantities.get(name)
        if qc is None or qc.support == UNSUPPORTED:
            continue
        cells = {c.hour: c for c in qc.diurnal}
        for p in points:
            if not p.eligible:
                continue
            v = p.values.get(name)
            if v is None:
                continue
            cell = cells.get(_hour_of(p.bucket_start))
            if cell is None or cell.p05 is None or cell.p95 is None:
                continue
            # A near-constant cell has p05 == p95, and then every window with
            # any float jitter reads as "outside". On a degenerate fixture
            # that flagged 192 of 192 windows. Require the cell to have real
            # spread before anything can be outside it.
            level = max(abs(cell.p50 or 0.0), 1e-9)
            if (cell.p95 - cell.p05) <= DEGENERATE_SPREAD_FRACTION * level:
                continue
            margin = MIN_EXCEEDANCE_FRACTION * (cell.p95 - cell.p05)
            if v > cell.p95 + margin:
                direction = "above"
            elif v < cell.p05 - margin:
                direction = "below"
            else:
                continue
            out.append(Candidate(
                bucket_start=p.bucket_start, quantity=name, value=v,
                direction=direction, cell_p05=cell.p05, cell_p95=cell.p95,
                cell_n_days=cell.n_days, support=qc.support,
            ))
    out.sort(key=lambda c: (c.bucket_start, c.quantity))
    return out


#: A 5th-95th percentile rule marks a tenth of everything by construction.
NOMINAL_CANDIDATE_RATE = 0.10


def candidate_summary(points: list, clim: Climatology, cands: list) -> dict:
    """Observed candidate rate against the rate the rule produces by design.

    This exists because "852 candidates" reads as 852 anomalies, and it is
    not. Marking anything outside the 5th-95th percentile flags a tenth of
    all windows when nothing whatsoever is happening -- that is what those
    percentiles mean. The interpretable figure is the *excess* over the
    nominal rate, and even that is noisy when each hour cell is estimated
    from a handful of day replicates.

    Reported on the page next to the count, so the count cannot be read alone.
    """
    scored = [n for n, q in clim.quantities.items() if q.support != UNSUPPORTED]
    eligible = [p for p in points if p.eligible]
    pairs = sum(
        1 for p in eligible for n in scored if p.values.get(n) is not None)
    # With nothing scored there is no rate. Reporting 0.0 (and an excess of
    # -0.1) would read as "well below normal" when it means "not measured".
    observed = (len(cands) / pairs) if pairs else None
    return {
        "candidates": len(cands),
        "scored_pairs": pairs,
        "scored_quantities": sorted(scored),
        "observed_rate": round(observed, 4) if observed is not None else None,
        "nominal_rate": NOMINAL_CANDIDATE_RATE,
        "excess_over_nominal": (round(observed - NOMINAL_CANDIDATE_RATE, 4)
                                if observed is not None else None),
        "note": (
            "A 5th-95th percentile rule marks 10% of windows by construction, "
            "so the count is mostly the definition. Two things bias the "
            "comparison and neither means the network was calm: the "
            "percentiles are fitted to the same windows they score, and the "
            f"{MIN_EXCEEDANCE_FRACTION:.0%}-of-band margin drops marginal "
            "exceedances, which pushes the observed rate BELOW 10%. A "
            "negative excess is therefore expected. Read it as a rough "
            "orientation, not a measurement, and read it cautiously: with few "
            "day replicates per hour cell the tail estimates are themselves "
            "unstable."
        ),
    }
