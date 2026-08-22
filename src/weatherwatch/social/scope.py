"""The evidence / interpretation split, expressed as an API rather than a rule.

Every sensor in this package is two functions with different arguments:

    select(source, scope)            -> EvidenceSet      # NO config parameter
    interpret(evidence, config)      -> list[Finding]    # config only here

That signature is the whole discipline. Evidence selection is a function of
the observation window alone, so *what was looked at* cannot be steered by
thresholds. Only *what was concluded* can. Consequences:

* `EvidenceSet.evidence_id` is invariant under any analysis config. Re-run
  with different thresholds and the evidence commitment is byte-identical.
* Because `select()` returns the complete scope rather than the part that
  scored well, the receipt commits to the data that did *not* support a
  finding too. That is what makes cherry-picking detectable on replay.
* A finding's own identity (`episode_id`) tracks its evidence segment, not
  its config. Two configs that select the same segment produce the same
  episode with different `config_hash`; configs that segment differently
  produce different episodes, which is the honest answer.

`DetectionEnvelope.window_fingerprint` is a joint data+config commitment by
driftwatch's own definition, so it moves when config moves. `evidence_id` is
the pure-evidence one. Both are carried; neither is redefined.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .envelope import (
    EvidenceStub,
    SubjectRef,
    build_envelope,
    make_hashset_root,
    make_note,
    receipt_hash,
)

#: Every detector reports magnitude on one scale: **log2 of a ratio against
#: its own null**, so one unit is one doubling. 1.0 means twice the baseline
#: rate / twice the null concentration / twice the null compression.
#:
#: This deliberately separates two questions that a single z-score conflates:
#:
#:   * *Is this an episode?*  -> decided by the detector's z / threshold gate.
#:   * *How big was it?*      -> decided here, by the size of the effect.
#:
#: The separation was forced by real data. On the local weather database a
#: five-minute block.create departure of 3.5x baseline scored z = 83, because
#: a median-absolute-deviation baseline over a smooth, strongly autocorrelated
#: minute series has almost no scale. Reporting z as magnitude put 17 of 41
#: episodes in the top band, which is an instrument that cannot tell a tremor
#: from an earthquake. The ratio says 3.5x and stops.
#:
#: This is a Richter reading, not a verdict. "critical" means the ground moved
#: a long way. It does not mean anyone did anything wrong, and no consumer of
#: these envelopes is entitled to read it that way. The envelope's severity
#: vocabulary is fixed by the vendored schema, so the honest move is to say
#: plainly what the five words mean here rather than to invent a sixth.
#:
#: THE BANDS ARE PROVISIONAL. They are round numbers chosen before any
#: calibration against a long observation, not thresholds derived from one.
#: `explain` carries the underlying ratio and z on every finding precisely so
#: a reader can ignore these bands and use their own.
MAGNITUDE_BANDS = (
    (3.5, "critical"),   # >= ~11x
    (2.0, "high"),       # ~4x - 11x
    (1.0, "med"),        # 2x - 4x
    (0.585, "low"),      # ~1.5x - 2x
)


def magnitude(ratio: float) -> float:
    """log2 of a ratio against the detector's null. 0.0 means "no departure"."""
    import math
    if ratio is None or ratio <= 0:
        return 0.0
    return round(math.log2(max(ratio, 1.0)), 6)


def magnitude_band(score: float) -> str:
    for floor, name in MAGNITUDE_BANDS:
        if score >= floor:
            return name
    return "info"


@dataclass(frozen=True)
class Scope:
    """What was looked at. Contains no thresholds, by construction.

    Adding a config-ish field here is the one edit that would quietly break
    the invariant, so `test_boundaries.py` asserts the field set.
    """

    kind: str                 # "aggregate" | "edge" | "lifecycle"
    subject_class: str        # metric name or collection alias, e.g. "block.create"
    ts_start: str             # ISO, inclusive
    ts_end: str               # ISO, exclusive
    window: str               # nominal bucket/window width, e.g. "60s"
    source: str               # endpoint or store identity

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def scope_id(self) -> str:
        return receipt_hash(self.as_dict())


@dataclass(frozen=True)
class EvidenceSet:
    """Everything in scope, plus a commitment to it.

    `receipts` are content-addressed per observed unit (a window in the
    aggregate tier, an edge record in the edge tier). `facts` are structural
    quantities derived from those units by config-free arithmetic — counts,
    Herfindahl, Jaccard. Interpretation reads `facts`; it never re-selects.
    """

    scope: Scope
    receipts: tuple[str, ...]
    facts: dict = field(default_factory=dict)
    #: Rows kept in memory for interpretation. Never hashed into identity and
    #: never persisted from here.
    payload: tuple = field(default=(), repr=False, compare=False)

    @property
    def evidence_id(self) -> str:
        return receipt_hash({
            "scope": self.scope.as_dict(),
            "receipts": sorted(self.receipts),
            "n": len(self.receipts),
        })

    def scope_stub(self) -> EvidenceStub:
        return make_hashset_root(list(self.receipts), len(self.receipts))


@dataclass(frozen=True)
class Finding:
    """One interpretation of part of an EvidenceSet."""

    type: str
    ts_start: str
    ts_end: str
    score: float
    explain: dict
    segment_receipts: tuple[str, ...]

    @property
    def episode_id(self) -> str:
        """Identity of the episode claim. Tracks evidence, not thresholds."""
        return receipt_hash({
            "type": self.type,
            "segment": sorted(self.segment_receipts),
        })


@dataclass(frozen=True)
class AnalysisConfig:
    """Base for detector configs. Subclass with plain fields; nothing else."""

    @property
    def config_hash(self) -> str:
        return receipt_hash({"config": asdict(self), "class": type(self).__name__})


def seal(
    detector_id: str,
    detector_version: str,
    evidence: EvidenceSet,
    finding: Finding,
    config: AnalysisConfig,
    watermark: dict | None = None,
    extra_evidence: tuple[EvidenceStub, ...] = (),
):
    """Turn (evidence, finding, config) into a sealed DetectionEnvelope.

    The subject is always `("episode", episode_id)`. There is no code path in
    this package that constructs a `("did", ...)` subject, and a test asserts
    it stays that way.
    """
    from .envelope import compute_window_fingerprint

    cfg_hash = config.config_hash
    wf = compute_window_fingerprint(
        ts_start=finding.ts_start,
        ts_end=finding.ts_end,
        window=evidence.scope.window,
        schema_version=1,
        config_hash=cfg_hash,
        watermark_snapshot=watermark or {},
    )

    explain = dict(finding.explain)
    # Hashes, not identity: both are safe to carry on the envelope surface and
    # both are what a replay needs to find its way back to the data.
    explain["evidence_id"] = evidence.evidence_id
    explain["scope_id"] = evidence.scope.scope_id

    stubs = (
        evidence.scope_stub(),
        make_hashset_root(list(finding.segment_receipts),
                          len(finding.segment_receipts)),
        make_note(f"scope={evidence.scope.kind}:{evidence.scope.subject_class} "
                  f"window={evidence.scope.window}"),
    ) + tuple(extra_evidence)

    return build_envelope(
        detector_id=detector_id,
        detector_version=detector_version,
        ts_start=finding.ts_start,
        ts_end=finding.ts_end,
        window=evidence.scope.window,
        subject=SubjectRef("episode", finding.episode_id),
        detection_type=finding.type,
        score=finding.score,
        severity=magnitude_band(finding.score),
        explain=explain,
        evidence=stubs,
        window_fingerprint=wf,
        config_hash=cfg_hash,
    )
