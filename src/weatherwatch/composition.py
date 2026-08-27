"""Temporal composition of already-reduced facts, with no identity join.

This module is intentionally not a generic analytics engine.  It accepts a
closed document shape, validates every source contract and fact, selects facts
only by an explicit semantic measurement required by an installed rule, and
relates them only by their time windows.  There is no join-key field, no
free-form dimension, no expression language, and no caller-supplied prose.

Composition is a *candidate claim projection*.  It does not admit source
evidence, confer publication authority, or turn temporal association into
causation.  Those limits travel in every output document.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA_BUNDLE = "weatherwatch.composition.bundle/v1"
SCHEMA_CONTRACTS = "weatherwatch.composition.contracts/v1"
SCHEMA_OUTPUT = "weatherwatch.composition.output/v1"
SCHEMA_REFUSAL = "weatherwatch.composition.refusal/v1"
DEFAULT_CONTRACTS_PATH = (
    Path(__file__).resolve().parents[2] / ".ops" / "composition-contracts.json"
)

PRESENT = "PRESENT"
DEGRADED = "DEGRADED"
UNKNOWN = "UNKNOWN"
STALE = "STALE"
ABSENT = "ABSENT"
REFUSED = "REFUSED"
STATES = frozenset({PRESENT, DEGRADED, UNKNOWN, STALE, ABSENT, REFUSED})
USABLE_STATES = frozenset({PRESENT, DEGRADED})

# Weakest first.  The compositor never unions two partial envelopes into a
# stronger one: state and coverage fraction both take the weaker input.
STATE_STRENGTH = {
    REFUSED: 0,
    ABSENT: 1,
    UNKNOWN: 2,
    STALE: 3,
    DEGRADED: 4,
    PRESENT: 5,
}

TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")
DIMENSION_TOKEN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
IDENTITY_VALUE_PATTERNS = (
    re.compile(r"did:(?:plc|web|key):", re.I),
    re.compile(r"at://", re.I),
    re.compile(r"https?://", re.I),
    re.compile(r"\bbafy[a-z0-9]{10,}", re.I),
    re.compile(r"\ba:[0-9a-f]{12}\b", re.I),
    # Opaque stable hashes are not a privacy escape hatch.
    re.compile(r"\b[0-9a-f]{16,}\b", re.I),
)
IDENTITY_DIMENSION_WORDS = frozenset({
    "actor", "account", "did", "handle", "host", "hostname", "identity",
    "key", "person", "pseudonym", "repo", "subject", "token", "uri",
    "user", "hash",
})

# Contracts may select from these vocabularies; they cannot create new
# dimension names or values.  This makes a caller-supplied `cohort=alice` (or
# a prettier pseudonym) structurally unavailable rather than merely frowned
# upon.  New dimensions require a code and test change.
INSTALLED_DIMENSIONS: Mapping[str, frozenset[str]] = {
    "record_family": frozenset({"app_bsky", "other"}),
    "temporal_phase": frozenset({"before", "during", "after"}),
    "service_state": frozenset({"available", "degraded", "unavailable"}),
}


class CompositionRefused(ValueError):
    """A stable, non-echoing refusal safe to expose to callers."""

    def __init__(self, code: str, path: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.path = path
        self.reason = reason

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA_REFUSAL,
            "disposition": REFUSED,
            "code": self.code,
            "path": self.path,
            "reason": self.reason,
            "rejected_value_echoed": False,
        }


@dataclass(frozen=True)
class Measurement:
    measurement_id: str
    semantic_profile: str
    unit: str


@dataclass(frozen=True)
class SourceContract:
    contract_id: str
    producer_id: str
    vantage_profile: str
    coverage_profile: str
    measurements: Mapping[str, Measurement]
    allowed_dimensions: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class Fact:
    contract: SourceContract
    measurement: Measurement
    start: dt.datetime
    end: dt.datetime
    acquired_at: dt.datetime
    state: str
    coverage_fraction: float | None
    value: float | None
    dimensions: Mapping[str, str]

    def public_input(self) -> dict:
        return {
            "contract_id": self.contract.contract_id,
            "producer_id": self.contract.producer_id,
            "vantage_profile": self.contract.vantage_profile,
            "measurement": self.measurement.measurement_id,
            "semantic_profile": self.measurement.semantic_profile,
            "unit": self.measurement.unit,
            "window": {"start": _iso(self.start), "end": _iso(self.end)},
            "acquired_at": _iso(self.acquired_at),
            "state": self.state,
            "coverage_fraction": self.coverage_fraction,
            "dimensions": dict(sorted(self.dimensions.items())),
            "value": self.value,
        }


@dataclass(frozen=True)
class Rule:
    rule_id: str
    left: str
    right: str
    operation: str
    proposition: str
    permitted: tuple[str, ...]
    forbidden: tuple[str, ...]
    min_pairs: int = 1


RULES: dict[str, Rule] = {
    "weatherwatch.rule.production-search-association.v1": Rule(
        rule_id="weatherwatch.rule.production-search-association.v1",
        left="atproto.public_record_writes",
        right="external.normalized_search_interest",
        operation="pearson_exact_windows",
        proposition="weatherwatch.claim.production-search-temporal-association.v1",
        permitted=(
            "descriptive association between exactly aligned source windows",
            "the direction and magnitude of a Pearson coefficient",
        ),
        forbidden=(
            "attention", "engagement", "audience size", "causation",
            "actor-level inference",
        ),
        min_pairs=3,
    ),
    "weatherwatch.rule.block-label-proximity.v1": Rule(
        rule_id="weatherwatch.rule.block-label-proximity.v1",
        left="atproto.block_record_writes",
        right="labelwatch.label_issuance",
        operation="temporal_overlap",
        proposition="weatherwatch.claim.block-label-temporal-overlap.v1",
        permitted=("temporal overlap in bounded source windows",),
        forbidden=(
            "moderation caused blocking", "blocking caused moderation",
            "intent", "coordination", "actor-level inference",
        ),
    ),
    "weatherwatch.rule.production-plc-proximity.v1": Rule(
        rule_id="weatherwatch.rule.production-plc-proximity.v1",
        left="atproto.public_record_writes",
        right="plc.directory.operations",
        operation="temporal_overlap",
        proposition="weatherwatch.claim.production-plc-temporal-overlap.v1",
        permitted=("temporal overlap in bounded source windows",),
        forbidden=(
            "network population", "daily active users", "production per capita",
            "migrant follow-through", "actor-level inference", "causation",
        ),
    ),
}


def rules_document() -> dict:
    return {
        "schema": "weatherwatch.composition.rules/v1",
        "rules": [{
            "rule_id": rule.rule_id,
            "inputs": [rule.left, rule.right],
            "operation": rule.operation,
            "proposition": rule.proposition,
            "permitted_interpretations": list(rule.permitted),
            "forbidden_interpretations": list(rule.forbidden),
        } for rule in RULES.values()],
    }


def _exact_keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    extra = set(value) - allowed
    missing = allowed - set(value)
    if extra:
        raise CompositionRefused(
            "UNDECLARED_FIELD", path,
            "document contains a field outside the closed contract shape")
    if missing:
        raise CompositionRefused(
            "MISSING_FIELD", path,
            "document omits a required contract field")


def _token(value: Any, path: str) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise CompositionRefused(
            "INVALID_SEMANTIC_ID", path,
            "semantic identifiers must use the bounded token syntax")
    if any(pattern.search(value) for pattern in IDENTITY_VALUE_PATTERNS):
        raise CompositionRefused(
            "IDENTITY_SHAPED_VALUE", path,
            "identity-shaped values cannot cross the reduction boundary")
    return value


def _dimension_token(value: Any, path: str, *, name: bool = False) -> str:
    if not isinstance(value, str) or not DIMENSION_TOKEN.fullmatch(value):
        raise CompositionRefused(
            "UNBOUNDED_DIMENSION", path,
            "dimensions must be short values from a declared finite vocabulary")
    parts = set(value.split("_"))
    if parts & IDENTITY_DIMENSION_WORDS:
        raise CompositionRefused(
            "IDENTITY_DIMENSION", path,
            "actor-shaped and pseudonymous dimensions are forbidden")
    if any(pattern.search(value) for pattern in IDENTITY_VALUE_PATTERNS):
        raise CompositionRefused(
            "IDENTITY_SHAPED_VALUE", path,
            "identity-shaped values cannot cross the reduction boundary")
    return value


def _timestamp(value: Any, path: str) -> dt.datetime:
    if not isinstance(value, str):
        raise CompositionRefused("INVALID_WINDOW", path,
                                 "window timestamps must be timezone-aware strings")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompositionRefused("INVALID_WINDOW", path,
                                 "window timestamp is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise CompositionRefused("INVALID_WINDOW", path,
                                 "window timestamps must carry a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompositionRefused("INVALID_VALUE", path,
                                 "fact values must be finite numbers")
    out = float(value)
    if not math.isfinite(out):
        raise CompositionRefused("INVALID_VALUE", path,
                                 "fact values must be finite numbers")
    return out


def _parse_contract(raw: Any, index: int) -> SourceContract:
    path = f"contracts[{index}]"
    if not isinstance(raw, Mapping):
        raise CompositionRefused("INVALID_CONTRACT", path,
                                 "source contract must be an object")
    _exact_keys(raw, {
        "contract_id", "producer_id", "vantage_profile", "coverage_profile",
        "measurements", "allowed_dimensions",
    }, path)
    contract_id = _token(raw["contract_id"], f"{path}.contract_id")
    producer_id = _token(raw["producer_id"], f"{path}.producer_id")
    vantage = _token(raw["vantage_profile"], f"{path}.vantage_profile")
    coverage = _token(raw["coverage_profile"], f"{path}.coverage_profile")

    if not isinstance(raw["measurements"], Mapping) or not raw["measurements"]:
        raise CompositionRefused("INVALID_CONTRACT", f"{path}.measurements",
                                 "contract must declare at least one measurement")
    measurements: dict[str, Measurement] = {}
    for measurement_id, spec in raw["measurements"].items():
        mid = _token(measurement_id, f"{path}.measurements")
        if not isinstance(spec, Mapping):
            raise CompositionRefused("INVALID_CONTRACT", f"{path}.measurements",
                                     "measurement specification must be an object")
        _exact_keys(spec, {"semantic_profile", "unit"},
                    f"{path}.measurements.{mid}")
        measurements[mid] = Measurement(
            mid,
            _token(spec["semantic_profile"],
                   f"{path}.measurements.{mid}.semantic_profile"),
            _dimension_token(spec["unit"], f"{path}.measurements.{mid}.unit"),
        )

    if not isinstance(raw["allowed_dimensions"], Mapping):
        raise CompositionRefused("INVALID_CONTRACT", f"{path}.allowed_dimensions",
                                 "allowed_dimensions must be an object")
    dimensions: dict[str, frozenset[str]] = {}
    for name, values in raw["allowed_dimensions"].items():
        dim = _dimension_token(name, f"{path}.allowed_dimensions", name=True)
        installed = INSTALLED_DIMENSIONS.get(dim)
        if installed is None:
            raise CompositionRefused(
                "DIMENSION_PROFILE_NOT_INSTALLED",
                f"{path}.allowed_dimensions.{dim}",
                "source contracts can use only installed finite dimensions")
        if (not isinstance(values, list) or not values or len(values) > 32):
            raise CompositionRefused(
                "UNBOUNDED_DIMENSION", f"{path}.allowed_dimensions.{dim}",
                "dimension vocabulary must contain between 1 and 32 values")
        allowed = frozenset(
            _dimension_token(item, f"{path}.allowed_dimensions.{dim}")
            for item in values)
        if len(allowed) != len(values):
            raise CompositionRefused(
                "INVALID_CONTRACT", f"{path}.allowed_dimensions.{dim}",
                "dimension vocabulary contains duplicates")
        if not allowed <= installed:
            raise CompositionRefused(
                "UNDECLARED_DIMENSION_VALUE",
                f"{path}.allowed_dimensions.{dim}",
                "source contract dimension is outside the installed vocabulary")
        dimensions[dim] = allowed
    return SourceContract(contract_id, producer_id, vantage, coverage,
                          measurements, dimensions)


def _parse_fact(raw: Any, index: int,
                contracts: Mapping[str, SourceContract]) -> Fact:
    path = f"facts[{index}]"
    if not isinstance(raw, Mapping):
        raise CompositionRefused("INVALID_FACT", path, "fact must be an object")
    _exact_keys(raw, {
        "contract_id", "measurement", "window", "state",
        "acquired_at", "coverage_fraction", "value", "unit", "dimensions",
    }, path)
    contract_id = _token(raw["contract_id"], f"{path}.contract_id")
    contract = contracts.get(contract_id)
    if contract is None:
        raise CompositionRefused("UNDECLARED_SOURCE", f"{path}.contract_id",
                                 "fact names no declared source contract")
    measurement_id = _token(raw["measurement"], f"{path}.measurement")
    measurement = contract.measurements.get(measurement_id)
    if measurement is None:
        raise CompositionRefused("SEMANTIC_MISMATCH", f"{path}.measurement",
                                 "measurement is not permitted by its source contract")
    if raw["unit"] != measurement.unit:
        raise CompositionRefused("SEMANTIC_MISMATCH", f"{path}.unit",
                                 "fact unit does not match the source contract")

    window = raw["window"]
    if not isinstance(window, Mapping):
        raise CompositionRefused("INVALID_WINDOW", f"{path}.window",
                                 "window must be an object")
    _exact_keys(window, {"start", "end"}, f"{path}.window")
    start = _timestamp(window["start"], f"{path}.window.start")
    end = _timestamp(window["end"], f"{path}.window.end")
    if start >= end:
        raise CompositionRefused("INVALID_WINDOW", f"{path}.window",
                                 "window end must be later than window start")
    acquired_at = _timestamp(raw["acquired_at"], f"{path}.acquired_at")
    if acquired_at < start:
        raise CompositionRefused(
            "ACQUISITION_PRECEDES_EVENT_WINDOW", f"{path}.acquired_at",
            "a fact cannot be acquired before its event-time window begins")

    state = raw["state"]
    if state not in STATES:
        raise CompositionRefused("INVALID_STATE", f"{path}.state",
                                 "fact state is outside the closed validity vocabulary")
    if state in USABLE_STATES:
        value = _number(raw["value"], f"{path}.value")
        fraction = _number(raw["coverage_fraction"],
                           f"{path}.coverage_fraction")
        if not 0 <= fraction <= 1:
            raise CompositionRefused("INVALID_COVERAGE",
                                     f"{path}.coverage_fraction",
                                     "coverage fraction must be between zero and one")
        if state == PRESENT and fraction != 1:
            raise CompositionRefused("INVALID_COVERAGE",
                                     f"{path}.coverage_fraction",
                                     "PRESENT requires complete declared coverage")
    else:
        if raw["value"] is not None or raw["coverage_fraction"] is not None:
            raise CompositionRefused(
                "HISTORICAL_VALUE_AS_CURRENT", path,
                "non-current facts cannot carry a value into composition")
        value = fraction = None

    if not isinstance(raw["dimensions"], Mapping):
        raise CompositionRefused("UNBOUNDED_DIMENSION", f"{path}.dimensions",
                                 "fact dimensions must be an object")
    dimensions: dict[str, str] = {}
    for name, dimension_value in raw["dimensions"].items():
        dim = _dimension_token(name, f"{path}.dimensions", name=True)
        allowed = contract.allowed_dimensions.get(dim)
        if allowed is None:
            raise CompositionRefused(
                "UNDECLARED_DIMENSION", f"{path}.dimensions.{dim}",
                "fact dimension is not declared by its source contract")
        val = _dimension_token(dimension_value, f"{path}.dimensions.{dim}")
        if val not in allowed:
            raise CompositionRefused(
                "UNDECLARED_DIMENSION_VALUE", f"{path}.dimensions.{dim}",
                "fact dimension value is outside the finite contract vocabulary")
        dimensions[dim] = val
    return Fact(contract, measurement, start, end, acquired_at, state,
                fraction, value, dimensions)


def validate_contracts(document: Any) -> dict[str, SourceContract]:
    if not isinstance(document, Mapping):
        raise CompositionRefused("INVALID_CONTRACT_REGISTRY", "contracts",
                                 "contract registry must be an object")
    _exact_keys(document, {"schema", "contracts"}, "contracts")
    if document["schema"] != SCHEMA_CONTRACTS:
        raise CompositionRefused("SCHEMA_MISMATCH", "contracts.schema",
                                 "unsupported contract registry schema")
    if not isinstance(document["contracts"], list):
        raise CompositionRefused("INVALID_CONTRACT_REGISTRY", "contracts.contracts",
                                 "contracts must be an array")
    contracts: dict[str, SourceContract] = {}
    for index, raw in enumerate(document["contracts"]):
        contract = _parse_contract(raw, index)
        if contract.contract_id in contracts:
            raise CompositionRefused("DUPLICATE_CONTRACT", f"contracts[{index}]",
                                     "source contract identifiers must be unique")
        contracts[contract.contract_id] = contract
    return contracts


def load_contracts(path: str | Path = DEFAULT_CONTRACTS_PATH) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompositionRefused(
            "CONTRACT_REGISTRY_UNREADABLE", "contracts",
            f"contract registry could not be read ({type(exc).__name__})") from exc
    return document


def validate_bundle(document: Any,
                    contracts_document: Any | None = None) -> tuple[dict[str, SourceContract], list[Fact]]:
    if not isinstance(document, Mapping):
        raise CompositionRefused("INVALID_BUNDLE", "$", "bundle must be an object")
    _exact_keys(document, {"schema", "facts"}, "$")
    if document["schema"] != SCHEMA_BUNDLE:
        raise CompositionRefused("SCHEMA_MISMATCH", "$.schema",
                                 "unsupported composition bundle schema")
    contracts = validate_contracts(
        contracts_document if contracts_document is not None else load_contracts())
    if not isinstance(document["facts"], list):
        raise CompositionRefused("INVALID_BUNDLE", "$.facts",
                                 "facts must be an array")
    facts = [_parse_fact(raw, index, contracts)
             for index, raw in enumerate(document["facts"])]
    seen: set[tuple] = set()
    for index, fact in enumerate(facts):
        identity = (
            fact.contract.contract_id,
            fact.measurement.measurement_id,
            fact.start,
            fact.end,
            tuple(sorted(fact.dimensions.items())),
        )
        if identity in seen:
            raise CompositionRefused(
                "DUPLICATE_FACT", f"facts[{index}]",
                "duplicate reduced facts cannot be merged or double-counted")
        seen.add(identity)
    return contracts, facts


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _weakest(facts: list[Fact]) -> tuple[str, float | None]:
    state = min((fact.state for fact in facts), key=STATE_STRENGTH.get)
    fractions = [fact.coverage_fraction for fact in facts
                 if fact.coverage_fraction is not None]
    return state, min(fractions) if fractions else None


def _base_output(rule: Rule) -> dict:
    return {
        "schema": SCHEMA_OUTPUT,
        "rule_id": rule.rule_id,
        "proposition": rule.proposition,
        "composition_authority": False,
        "source_admission_performed": False,
        "join_basis": "bounded_temporal_overlap_only",
        "permitted_interpretations": list(rule.permitted),
        "forbidden_interpretations": list(rule.forbidden),
        "claims": [],
    }


def _unsatisfied(rule: Rule, state: str, reason: str,
                 facts: list[Fact] | None = None) -> dict:
    output = _base_output(rule)
    output["disposition"] = "UNSATISFIED"
    output["state"] = state
    output["reason"] = reason
    output["inputs"] = [fact.public_input() for fact in (facts or [])]
    return output


def _overlap(rule: Rule, left: list[Fact], right: list[Fact]) -> dict:
    all_facts = left + right
    state, fraction = _weakest(all_facts)
    if state not in USABLE_STATES:
        return _unsatisfied(rule, state,
                            "at least one participating source is not currently usable",
                            all_facts)
    claims = []
    for a in left:
        for b in right:
            start, end = max(a.start, b.start), min(a.end, b.end)
            if start >= end:
                continue
            claims.append({
                "state": min((a.state, b.state), key=STATE_STRENGTH.get),
                "coverage": {
                    "state": min((a.state, b.state), key=STATE_STRENGTH.get),
                    "fraction": min(a.coverage_fraction, b.coverage_fraction),
                    "basis": "weakest participating envelope; never a union",
                },
                "relation": {
                    "kind": "simultaneous",
                    "overlap": {"start": _iso(start), "end": _iso(end)},
                },
                "inputs": [a.public_input(), b.public_input()],
                "statement": (
                    "The two declared measurements were present in overlapping "
                    "source windows. This licenses temporal proximity only."
                ),
            })
    if not claims:
        return _unsatisfied(rule, UNKNOWN,
                            "declared measurements have no temporal overlap",
                            all_facts)
    output = _base_output(rule)
    output.update({
        "disposition": "COMPOSED",
        "state": state,
        "coverage_fraction": fraction,
        "claims": claims,
    })
    return output


def _pearson(rule: Rule, left: list[Fact], right: list[Fact]) -> dict:
    all_facts = left + right
    state, fraction = _weakest(all_facts)
    if state not in USABLE_STATES:
        return _unsatisfied(rule, state,
                            "at least one participating source is not currently usable",
                            all_facts)
    for side, items in (("left", left), ("right", right)):
        windows = [(fact.start, fact.end) for fact in items]
        if len(windows) != len(set(windows)):
            raise CompositionRefused(
                "AMBIGUOUS_WINDOW", side,
                "correlation requires exactly one fact per measurement window")
    right_by_window = {(fact.start, fact.end): fact for fact in right}
    pairs = [(fact, right_by_window[(fact.start, fact.end)]) for fact in left
             if (fact.start, fact.end) in right_by_window]
    if len(pairs) < rule.min_pairs:
        return _unsatisfied(
            rule, UNKNOWN,
            f"rule requires at least {rule.min_pairs} exactly aligned windows",
            all_facts)
    xs = [pair[0].value for pair in pairs]
    ys = [pair[1].value for pair in pairs]
    assert all(value is not None for value in xs + ys)
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys))
    xsum = sum((x - xmean) ** 2 for x in xs)
    ysum = sum((y - ymean) ** 2 for y in ys)
    if xsum == 0 or ysum == 0:
        return _unsatisfied(rule, UNKNOWN,
                            "correlation is undefined for a constant series",
                            [fact for pair in pairs for fact in pair])
    coefficient = numerator / math.sqrt(xsum * ysum)
    inputs = [fact.public_input() for pair in pairs for fact in pair]
    output = _base_output(rule)
    output.update({
        "disposition": "COMPOSED",
        "state": state,
        "coverage_fraction": fraction,
        "claims": [{
            "state": state,
            "coverage": {
                "state": state,
                "fraction": fraction,
                "basis": "weakest participating envelope; never a union",
            },
            "relation": {
                "kind": "pearson_correlation",
                "coefficient": coefficient,
                "aligned_window_count": len(pairs),
                "alignment": "exact_start_and_end",
            },
            "inputs": inputs,
            "statement": (
                "The declared measurements had a descriptive Pearson "
                f"correlation of {coefficient:.3f} across {len(pairs)} exactly "
                "aligned windows. Causal direction is not established."
            ),
        }],
    })
    return output


def compose(document: Any, rule_id: str,
            contracts_document: Any | None = None) -> dict:
    """Validate and compose one bundle under one installed explicit rule."""
    rule = RULES.get(rule_id)
    if rule is None:
        raise CompositionRefused("RULE_NOT_INSTALLED", "rule_id",
                                 "arbitrary arithmetic and rules are forbidden")
    _contracts, facts = validate_bundle(document, contracts_document)
    left = [fact for fact in facts
            if fact.measurement.measurement_id == rule.left]
    right = [fact for fact in facts
             if fact.measurement.measurement_id == rule.right]
    if not left or not right:
        available = left + right
        return _unsatisfied(rule, ABSENT,
                            "one or more required measurements are absent",
                            available)
    if rule.operation == "temporal_overlap":
        return _overlap(rule, left, right)
    if rule.operation == "pearson_exact_windows":
        return _pearson(rule, left, right)
    raise CompositionRefused("RULE_INVALID", "rule_id",
                             "installed rule names no supported operation")
