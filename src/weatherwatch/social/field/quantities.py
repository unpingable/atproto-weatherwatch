"""The field vector: what is measured, and what each measurement refuses.

Every quantity carries its own `non_claim`. That is not decoration -- an
observation's non-claims are assembled from the quantities it actually
contains (`observation.py`), so the record states its limits in terms of what
it measured rather than a paragraph of boilerplate someone stops reading.

All of these are derived from `bucket` counts. None of them can be computed
per account, because the counters never held an account. That is the design
property: the absence is upstream of this module, not enforced by it.

Naming discipline, applied before the code was written: a quantity may
describe *composition* ("what share of graph events were boundary-forming")
and may not describe *disposition* ("how hostile"). `boundary_share` rising
means more block records relative to follow records were observed. It does
not mean conflict, hostility or a worsening climate, and the name is chosen so
that a reader cannot mistake it for one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ... import query

#: Every metric the field vector reads. All present in `classify`'s finite
#: alphabet; none of them carries identity.
FIELD_METRICS: tuple[str, ...] = (
    "post.create", "post.create.reply", "post.create.quote",
    "repost.create", "like.create",
    "follow.create", "follow.delete",
    "block.create", "block.delete",
    "listitem.create", "listitem.delete",
    "post.delete", "like.delete", "repost.delete",
)

#: Reaction events -- things done *to* content someone else emitted.
INTERACTION_METRICS = (
    "post.create.reply", "post.create.quote", "repost.create", "like.create",
)

#: Create/delete pairs where withdrawal is meaningful.
CREATE_DELETE_PAIRS = (
    ("post.create", "post.delete"),
    ("like.create", "like.delete"),
    ("repost.create", "repost.delete"),
    ("follow.create", "follow.delete"),
    ("block.create", "block.delete"),
    ("listitem.create", "listitem.delete"),
)


@dataclass(frozen=True)
class Quantity:
    name: str
    unit: str
    summary: str
    non_claim: str
    #: Windows of trailing context needed. 0 = computable from one window.
    context_windows: int = 0


QUANTITIES: tuple[Quantity, ...] = (
    Quantity(
        "emission_velocity", "events/s",
        "Rate at which new top-level content records were observed.",
        "Not a measure of how many people are talking: one repo may emit "
        "many records and the counters cannot tell them apart.",
    ),
    Quantity(
        "interaction_velocity", "events/s",
        "Rate of reaction records observed -- replies, quotes, reposts, likes.",
        "Not engagement quality, not sentiment, and not attention: a like and "
        "a hostile quote are one event each here.",
    ),
    Quantity(
        "interaction_pressure", "reactions/post",
        "Reaction records per unit of emitted content in the same window.",
        "Not a controversy score. High pressure is equally consistent with a "
        "popular post, a slow news minute, and a quiet emission rate.",
    ),
    Quantity(
        "reply_share", "ratio",
        "Share of reaction records that were replies rather than likes, "
        "reposts or quotes.",
        "Not a measure of argument. Replies include agreement, jokes and "
        "threads an author writes to themselves.",
    ),
    Quantity(
        "graph_velocity", "events/s",
        "Rate of graph-mutating records: follow, block and list membership, "
        "created or deleted.",
        "Counts graph *events*, never a graph. No edge is retained and no "
        "endpoint is known.",
    ),
    Quantity(
        "boundary_share", "ratio",
        "Share of new follow-or-block records that were blocks.",
        "Not conflict, hostility, or a worsening climate. Blocks are also "
        "routine hygiene, list-driven, and often unrelated to any dispute.",
    ),
    Quantity(
        "withdrawal_share", "ratio",
        "Share of create-or-delete records across tracked collections that "
        "were deletions.",
        "Not regret, retraction or censorship. Deletion is also editing, "
        "cleanup, migration and automation.",
    ),
    Quantity(
        "turbulence", "coefficient of variation",
        "Variability of interaction velocity across the trailing context, "
        "relative to its own mean.",
        "Not instability of a community. It is a property of the observed "
        "rate series at one endpoint.",
        context_windows=10,
    ),
    Quantity(
        "acceleration", "events/s per window",
        "Change in interaction velocity from the previous eligible window.",
        "Not causation and not a trend: one window's difference, nothing "
        "about what produced it.",
        context_windows=1,
    ),
)

QUANTITY_BY_NAME = {q.name: q for q in QUANTITIES}
QUANTITY_NAMES = tuple(q.name for q in QUANTITIES)


@dataclass(frozen=True)
class FieldPoint:
    """One window of the field. `values` may hold None for any quantity."""

    bucket_start: int
    bucket_width: int
    observed: bool
    eligible: bool
    quality: str
    observed_seconds: float
    values: dict


def _rate(point: query.WindowPoint | None) -> float | None:
    return point.rate if point is not None else None


def _sum_rates(points: list) -> float | None:
    vals = [p for p in points if p is not None]
    return sum(vals) if vals else None


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _share(part: float | None, whole_parts: list) -> float | None:
    if part is None:
        return None
    vals = [v for v in whole_parts if v is not None]
    total = sum(vals)
    if total <= 0:
        return None
    return part / total


def _stdev(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def build_field(series_map: dict) -> list[FieldPoint]:
    """Per-window field vectors from an aligned map of metric -> Series.

    Windows the collector did not observe yield a FieldPoint with every value
    None and `observed=False`. They are never dropped and never zero-filled:
    a hole in observation is a fact about the instrument, and a climatology
    built over silently-filled holes would describe the filling.
    """
    by_metric = {
        m: {p.bucket_start: p for p in s.points} for m, s in series_map.items()
    }
    starts = sorted({b for m in by_metric.values() for b in m})
    if not starts:
        return []
    width = next(iter(series_map.values())).bucket_width

    # First pass: the per-window quantities.
    points: list[FieldPoint] = []
    for b in starts:
        got = {m: by_metric.get(m, {}).get(b) for m in FIELD_METRICS}
        anchor = next((p for p in got.values() if p is not None), None)
        observed = bool(anchor and anchor.observed)
        eligible = bool(anchor and anchor.baseline_eligible)
        rates = {m: (_rate(p) if observed else None) for m, p in got.items()}

        emission = rates.get("post.create")
        inter_parts = [rates.get(m) for m in INTERACTION_METRICS]
        interaction = _sum_rates(inter_parts)
        graph_parts = [rates.get(m) for m in (
            "follow.create", "follow.delete", "block.create", "block.delete",
            "listitem.create", "listitem.delete")]
        creates = [rates.get(c) for c, _ in CREATE_DELETE_PAIRS]
        deletes = [rates.get(d) for _, d in CREATE_DELETE_PAIRS]
        delete_total = _sum_rates(deletes)
        create_total = _sum_rates(creates)

        values = {
            "emission_velocity": emission,
            "interaction_velocity": interaction,
            "interaction_pressure": _ratio(interaction, emission),
            "reply_share": _share(rates.get("post.create.reply"), inter_parts),
            "graph_velocity": _sum_rates(graph_parts),
            "boundary_share": _share(
                rates.get("block.create"),
                [rates.get("block.create"), rates.get("follow.create")]),
            "withdrawal_share": _share(
                delete_total, [delete_total, create_total]),
            "turbulence": None,      # needs context; second pass
            "acceleration": None,
        }
        points.append(FieldPoint(
            bucket_start=b, bucket_width=width, observed=observed,
            eligible=eligible,
            quality=(anchor.quality if anchor else "unobserved"),
            observed_seconds=(anchor.observed_seconds if anchor else 0.0),
            values=values,
        ))

    _attach_context(points)
    return points


def _attach_context(points: list[FieldPoint]) -> None:
    """Turbulence and acceleration, from trailing *eligible* windows only.

    Trailing context steps over ineligible windows rather than filling them,
    so a degraded stretch makes the context reach further back in wall-clock
    time instead of inventing values -- the same rule the weather lane's own
    rolling baselines follow.
    """
    ctx = QUANTITY_BY_NAME["turbulence"].context_windows
    history: list[float] = []
    prev: float | None = None
    for p in points:
        v = p.values["interaction_velocity"]
        # Both quantities describe conditions *at this window*, so an
        # unobserved window gets neither -- however much trailing context
        # happens to be available. Deriving them from history alone would put
        # a real-looking number on a window nobody watched, which is the one
        # thing this estate does not do.
        if p.observed:
            if history:
                window = history[-ctx:]
                mean = sum(window) / len(window)
                sd = _stdev(window)
                if sd is not None and mean > 0:
                    p.values["turbulence"] = sd / mean
            if v is not None and prev is not None:
                p.values["acceleration"] = v - prev
        if p.eligible and v is not None:
            history.append(v)
            prev = v


def non_claims_for(present: list) -> tuple:
    """The refusals that belong to the quantities actually measured."""
    out = [f"{n}: {QUANTITY_BY_NAME[n].non_claim}"
           for n in present if n in QUANTITY_BY_NAME]
    out.append(
        "No quantity here is attributable to any account, group or place. "
        "The counters these are built from never contained an actor, a "
        "target, a record body or a location."
    )
    out.append(
        "Co-occurrence in a window is not causation, and a departure from "
        "climatology is not a finding about anyone."
    )
    return tuple(out)
