"""Identity-bearing PLC export reduction into identity-free window facts.

The input side of this module intentionally sees DID PLC identifiers, CIDs,
keys, handles, and service endpoints.  None of those values has a field in the
result type.  The only cross-row identity state is an in-memory mapping used to
compare consecutive operations for one DID; it is cleared before returning or
refusing and is never serialized.

This is a batch reducer for the sequenced ``plc.directory/export?after=0``
JSONL shape.  It is not a PLC mirror, signature verifier, migration verifier,
population counter, or identity store.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import signal
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urlsplit

from . import composition


SCHEMA_REDUCTION = "weatherwatch.plc.reduction/v1"
SCHEMA_REFUSAL = "weatherwatch.plc.reduction-refusal/v1"
INPUT_FORMAT = "plc.directory.sequenced-op-jsonl/v1"
CONTRACT_ID = "plc.directory.operations.v1"
SOURCE_REPOSITORY = "https://github.com/did-method-plc/did-method-plc"
SOURCE_REVISION = "45a14801609e182afcd6907f95013cfb10381f73"
SOURCE_SPEC_PATH = "website/spec/v0.1/did-plc.md"
SOURCE_SPEC_SECTION = "Bulk Export"
SOURCE_SPEC_VERSION = "v0.3.0"
SOURCE_SPEC_SHA256 = "7346e2ba9d186fa13d65466942f29136f2d3e2281a8a6dd08d689eda2c99af79"

# Identity-derived counts are never emitted in five-minute or hourly cells.
# Non-overlapping UTC weeks and a bounded output horizon make an individual
# event unable to enter overlapping windows.  Within one measurement fact,
# counts below the threshold are indistinguishable.  Arithmetic across related
# facts or repeated acquisitions is explicitly outside that guarantee.
MIN_WINDOW_SECONDS = 7 * 24 * 60 * 60
DEFAULT_WINDOW_SECONDS = MIN_WINDOW_SECONDS
MIN_DISCLOSURE_COUNT = 10
DEFAULT_DISCLOSURE_COUNT = MIN_DISCLOSURE_COUNT
MAX_EMITTED_WINDOWS = 104
MAX_LINE_BYTES = 32_768
# Defensive batch/resource bounds, not claims about upstream population size.
# V0 deliberately refuses instead of spilling identity history to disk.
MAX_ENDPOINT_BYTES = 2_048
MAX_JSON_NESTING = 64
MAX_OPERATIONS = 1_000_000
MAX_DISTINCT_IDENTITIES = 250_000
MAX_TRACKED_WINDOWS = 4_096
WINDOW_ANCHOR = dt.datetime(1970, 1, 5, tzinfo=dt.timezone.utc)  # Monday

DID_PLC = re.compile(r"^did:plc:[a-z2-7]{24}$")

MEASUREMENTS = (
    "plc.directory.operations",
    "plc.directory.creations",
    "plc.directory.tombstones",
    "plc.directory.endpoint_mutations",
    "plc.directory.migration_like_transitions",
)
TRANSITION_MEASUREMENTS = frozenset({
    "plc.directory.endpoint_mutations",
    "plc.directory.migration_like_transitions",
})


def _new_transient_history() -> dict[str, tuple[str, object | None]]:
    """Test seam for proving that every exit path clears identity history."""
    return {}


class PLCReductionRefused(ValueError):
    """Stable refusal which never repeats identity-bearing input."""

    def __init__(self, code: str, path: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.path = path
        self.reason = reason

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA_REFUSAL,
            "disposition": "REFUSED",
            "code": self.code,
            "path": self.path,
            "reason": self.reason,
            "rejected_value_echoed": False,
            "raw_operation_retained": False,
        }


class PLCReductionInterrupted(PLCReductionRefused):
    """Controlled process-signal interruption under raw-input custody."""

    def __init__(self, signal_name: str, exit_code: int):
        super().__init__(
            f"INTERRUPTED_{signal_name}", "signal",
            f"PLC reduction was interrupted by {signal_name}")
        self.exit_code = exit_code


def parse_timestamp(value: str, path: str) -> dt.datetime:
    if not isinstance(value, str):
        raise PLCReductionRefused(
            "INVALID_TIMESTAMP", path,
            "timestamp must be a timezone-aware ISO-8601 string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PLCReductionRefused(
            "INVALID_TIMESTAMP", path,
            "timestamp is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PLCReductionRefused(
            "INVALID_TIMESTAMP", path, "timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def validate_policy(window_seconds: int, disclosure_count: int) -> None:
    if (isinstance(window_seconds, bool) or not isinstance(window_seconds, int)
            or window_seconds < MIN_WINDOW_SECONDS
            or window_seconds % MIN_WINDOW_SECONDS):
        raise PLCReductionRefused(
            "UNSAFE_WINDOW_POLICY", "policy.window_seconds",
            "PLC publication windows must be whole non-overlapping UTC weeks")
    if (isinstance(disclosure_count, bool)
            or not isinstance(disclosure_count, int)
            or disclosure_count < MIN_DISCLOSURE_COUNT):
        raise PLCReductionRefused(
            "UNSAFE_DISCLOSURE_POLICY", "policy.disclosure_count",
            "PLC disclosure threshold cannot be lower than the installed minimum")


def _window_start(value: dt.datetime, width: int) -> dt.datetime:
    elapsed = (value - WINDOW_ANCHOR).total_seconds()
    return WINDOW_ANCHOR + dt.timedelta(seconds=math.floor(elapsed / width) * width)


def _check_json_nesting(line: str, path: str) -> None:
    """Bound parser stack use without interpreting braces inside JSON strings."""
    depth = 0
    in_string = False
    escaped = False
    for character in line:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise PLCReductionRefused(
                    "JSON_NESTING_LIMIT_EXCEEDED", path,
                    "PLC export row exceeds the installed JSON nesting bound")
        elif character in "]}":
            depth = max(0, depth - 1)


def _exact_keys(value: Mapping[str, Any], required: set[str], path: str) -> None:
    if set(value) != required:
        raise PLCReductionRefused(
            "UNSUPPORTED_EXPORT_SHAPE", path,
            "sequenced export row does not match the closed official shape")


def _endpoint_state(operation: Mapping[str, Any]) -> tuple[str, object | None]:
    """Return transient comparison state; never place it in reducer output."""
    op_type = operation.get("type")
    endpoint: Any = None
    if op_type == "create":
        endpoint = operation.get("service")
    elif op_type == "plc_operation":
        services = operation.get("services")
        if isinstance(services, Mapping):
            service = services.get("atproto_pds")
            if isinstance(service, Mapping):
                endpoint = service.get("endpoint")
    if endpoint is None:
        return ("absent", None)
    if not isinstance(endpoint, str):
        return ("invalid", type(endpoint).__name__)
    if len(endpoint.encode("utf-8")) > MAX_ENDPOINT_BYTES:
        raise PLCReductionRefused(
            "ENDPOINT_TOO_LARGE", "operation.services.atproto_pds.endpoint",
            "PLC service endpoint exceeds the installed transient-state bound")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (ValueError, UnicodeError):
        return ("invalid", endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        return ("invalid", endpoint)
    # The origin is transient.  In particular it is never converted to a hash
    # or pseudonym and never crosses the return boundary.
    return ("qualified_origin", (parsed.hostname.lower(), port))


def _parse_row(raw: Any, line_number: int,
               prior_seq: int | None) -> tuple[int, dt.datetime, str, Mapping[str, Any]]:
    path = f"input.line[{line_number}]"
    if not isinstance(raw, Mapping):
        raise PLCReductionRefused(
            "UNSUPPORTED_EXPORT_SHAPE", path, "export row must be an object")
    _exact_keys(raw, {"type", "operation", "did", "cid", "createdAt", "seq"}, path)
    if raw["type"] != "sequenced_op":
        raise PLCReductionRefused(
            "LEGACY_EXPORT_REFUSED", f"{path}.type",
            "only the sequence-cursor PLC export is accepted")
    seq = raw["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise PLCReductionRefused(
            "INVALID_SEQUENCE", f"{path}.seq", "sequence must be a positive integer")
    if prior_seq is not None and seq <= prior_seq:
        raise PLCReductionRefused(
            "NON_MONOTONIC_SEQUENCE", f"{path}.seq",
            "sequenced export rows must be strictly monotonic")
    if not isinstance(raw["did"], str) or not DID_PLC.fullmatch(raw["did"]):
        raise PLCReductionRefused(
            "INVALID_PLC_IDENTITY", f"{path}.did",
            "row does not carry a syntactically valid did:plc identifier")
    if not isinstance(raw["cid"], str) or not raw["cid"]:
        raise PLCReductionRefused(
            "INVALID_OPERATION_CID", f"{path}.cid", "operation CID must be a string")
    operation = raw["operation"]
    if not isinstance(operation, Mapping):
        raise PLCReductionRefused(
            "INVALID_OPERATION", f"{path}.operation", "operation must be an object")
    op_type = operation.get("type")
    if op_type not in {"plc_operation", "plc_tombstone", "create"}:
        raise PLCReductionRefused(
            "UNSUPPORTED_OPERATION", f"{path}.operation.type",
            "operation type is outside the installed PLC profile")
    if op_type == "plc_operation" and not {"prev", "services"} <= set(operation):
        raise PLCReductionRefused(
            "INVALID_OPERATION", f"{path}.operation",
            "PLC operation omits fields required by the reducer")
    if op_type == "create" and not {"prev", "service"} <= set(operation):
        raise PLCReductionRefused(
            "INVALID_OPERATION", f"{path}.operation",
            "legacy creation omits fields required by the reducer")
    if op_type == "plc_tombstone" and "prev" not in operation:
        raise PLCReductionRefused(
            "INVALID_OPERATION", f"{path}.operation",
            "PLC tombstone omits its previous-operation pointer")
    return seq, parse_timestamp(raw["createdAt"], f"{path}.createdAt"), raw["did"], operation


def _fact(measurement: str, start: dt.datetime, end: dt.datetime,
          acquired_at: dt.datetime, count: int, disclosure_count: int,
          transition_coverage: float) -> dict:
    if count < disclosure_count:
        state, fraction, value = "UNKNOWN", None, None
    else:
        state = "DEGRADED"
        fraction = (transition_coverage
                    if measurement in TRANSITION_MEASUREMENTS else 1.0)
        value = count
    return {
        "contract_id": CONTRACT_ID,
        "measurement": measurement,
        "window": {"start": iso(start), "end": iso(end)},
        "acquired_at": iso(acquired_at),
        "state": state,
        "coverage_fraction": fraction,
        "value": value,
        "unit": "count",
        "dimensions": {},
    }


def reduce_jsonl(lines: Iterable[str], *, acquired_at: dt.datetime,
                 window_seconds: int = DEFAULT_WINDOW_SECONDS,
                 disclosure_count: int = DEFAULT_DISCLOSURE_COUNT,
                 core_dump_suppressed: bool = False) -> dict:
    """Reduce official sequenced PLC JSONL without retaining identity state."""
    validate_policy(window_seconds, disclosure_count)
    if acquired_at.tzinfo is None:
        raise PLCReductionRefused(
            "INVALID_TIMESTAMP", "acquired_at", "acquisition time must carry a timezone")
    acquired_at = acquired_at.astimezone(dt.timezone.utc)

    counts: dict[dt.datetime, dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    # DID and endpoint values exist only in this transient map.  It is cleared
    # in the finally block on success and on every handled refusal.
    previous = _new_transient_history()
    prior_seq: int | None = None
    first_seq: int | None = None
    entry_count = 0
    unresolved_updates = 0
    event_min: dt.datetime | None = None
    event_max: dt.datetime | None = None
    try:
        for line_number, line in enumerate(lines, 1):
            if line_number > MAX_OPERATIONS:
                raise PLCReductionRefused(
                    "OPERATION_LIMIT_EXCEEDED", "input",
                    "PLC batch exceeds the installed operation-count bound")
            if not isinstance(line, str):
                raise PLCReductionRefused(
                    "INVALID_INPUT", f"input.line[{line_number}]",
                    "PLC export must be decoded JSONL text")
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                raise PLCReductionRefused(
                    "OPERATION_TOO_LARGE", f"input.line[{line_number}]",
                    "PLC export row exceeds the installed input bound")
            if not line.strip():
                raise PLCReductionRefused(
                    "EMPTY_INPUT_LINE", f"input.line[{line_number}]",
                    "blank lines are not valid sequenced export rows")
            _check_json_nesting(line, f"input.line[{line_number}]")
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise PLCReductionRefused(
                    "MALFORMED_JSON", f"input.line[{line_number}]",
                    "PLC export row is not valid JSON") from exc
            seq, event_time, did, operation = _parse_row(raw, line_number, prior_seq)
            if event_time > acquired_at:
                raise PLCReductionRefused(
                    "EVENT_TIME_AFTER_ACQUISITION", f"input.line[{line_number}].createdAt",
                    "source event time cannot be later than acquisition time")
            first_seq = seq if first_seq is None else first_seq
            prior_seq = seq
            entry_count += 1
            event_min = event_time if event_min is None else min(event_min, event_time)
            event_max = event_time if event_max is None else max(event_max, event_time)
            window = _window_start(event_time, window_seconds)
            if window not in counts and len(counts) >= MAX_TRACKED_WINDOWS:
                raise PLCReductionRefused(
                    "WINDOW_LIMIT_EXCEEDED", "input",
                    "PLC batch exceeds the installed event-window bound")
            bucket = counts[window]
            bucket["plc.directory.operations"] += 1
            op_type = operation["type"]
            is_creation = op_type == "create" or (
                op_type == "plc_operation" and operation.get("prev") is None)
            if is_creation:
                bucket["plc.directory.creations"] += 1
            if op_type == "plc_tombstone":
                bucket["plc.directory.tombstones"] += 1

            current = _endpoint_state(operation)
            if did not in previous and len(previous) >= MAX_DISTINCT_IDENTITIES:
                raise PLCReductionRefused(
                    "IDENTITY_HISTORY_LIMIT_EXCEEDED", "input",
                    "PLC batch exceeds the installed transient-history bound")
            is_update = not is_creation and op_type == "plc_operation"
            if is_update:
                bucket["updates"] += 1
                if did not in previous:
                    unresolved_updates += 1
                else:
                    bucket["comparable_updates"] += 1
                    prior = previous[did]
                    if current != prior:
                        bucket["plc.directory.endpoint_mutations"] += 1
                        if (current[0] == prior[0] == "qualified_origin"
                                and current[1] != prior[1]):
                            bucket["plc.directory.migration_like_transitions"] += 1
            previous[did] = current
    finally:
        previous.clear()

    if entry_count == 0:
        raise PLCReductionRefused(
            "EMPTY_EXPORT", "input",
            "an empty export supplies no PLC observation to reduce")

    complete_windows = sorted(
        start for start in counts
        if start + dt.timedelta(seconds=window_seconds) <= acquired_at)
    retained_windows = complete_windows[-MAX_EMITTED_WINDOWS:]
    facts: list[dict] = []
    for start in retained_windows:
        end = start + dt.timedelta(seconds=window_seconds)
        bucket = counts[start]
        updates = bucket["updates"]
        comparable = bucket["comparable_updates"]
        transition_coverage = comparable / updates if updates else 1.0
        for measurement in MEASUREMENTS:
            facts.append(_fact(
                measurement, start, end, acquired_at,
                bucket[measurement], disclosure_count, transition_coverage))

    return {
        "schema": SCHEMA_REDUCTION,
        "disposition": "REDUCED",
        "reduction_authority": False,
        "publication_authority": False,
        "source_contract_id": CONTRACT_ID,
        "source_contract": {
            "upstream_repository": SOURCE_REPOSITORY,
            "revision": SOURCE_REVISION,
            "spec_path": SOURCE_SPEC_PATH,
            "spec_section": SOURCE_SPEC_SECTION,
            "spec_version": SOURCE_SPEC_VERSION,
            "spec_sha256": SOURCE_SPEC_SHA256,
        },
        "input_format": INPUT_FORMAT,
        "acquired_at": iso(acquired_at),
        "window_policy": {
            "window_seconds": window_seconds,
            "alignment": "non_overlapping_utc_weeks",
            "disclosure_count": disclosure_count,
            "zero_through_threshold_minus_one_indistinguishable": True,
            "maximum_emitted_windows": MAX_EMITTED_WINDOWS,
        },
        "resource_policy": {
            "maximum_input_line_bytes": MAX_LINE_BYTES,
            "maximum_endpoint_bytes": MAX_ENDPOINT_BYTES,
            "maximum_json_nesting": MAX_JSON_NESTING,
            "maximum_operations": MAX_OPERATIONS,
            "maximum_distinct_identity_histories": MAX_DISTINCT_IDENTITIES,
            "maximum_tracked_event_windows": MAX_TRACKED_WINDOWS,
            "spill_identity_state_to_disk": False,
        },
        "disclosure_claim": {
            "low_count_suppression_scope": "each_fact_independently",
            "compositional_non_disclosure_claimed": False,
            "revision_differencing_protected": False,
            "derived_small_aggregate_counts_possible": True,
            "persisted_identity_linkage_fields_present": False,
        },
        "source_envelope": {
            "entries_observed": entry_count,
            "first_sequence": first_seq,
            "last_sequence": prior_seq,
            "event_time_min": iso(event_min) if event_min else None,
            "event_time_max": iso(event_max) if event_max else None,
            "complete_windows_observed": len(complete_windows),
            "windows_emitted": len(retained_windows),
            "incomplete_current_windows_omitted": len(counts) - len(complete_windows),
            "unresolved_update_predecessors": unresolved_updates,
            "directory_completeness_claimed": False,
            "population_denominator_claimed": False,
            "nullification_status": "not_available_in_sequenced_export",
            "coverage_scope": "only accepted rows in the supplied sequenced export",
        },
        "custody": {
            "identity_bearing_input_ephemeral": True,
            "cross_row_identity_state": "memory_only_and_cleared",
            "raw_operations_retained": False,
            "identity_or_endpoint_emitted": False,
            "provider_dimensions_emitted": False,
            "stable_pseudonyms_emitted": False,
            "application_refusals_echo_input": False,
            "core_dump_suppressed_during_reduction": core_dump_suppressed,
            "secure_memory_erasure_claimed": False,
            "host_memory_confidentiality_claimed": False,
        },
        "semantic_limits": {
            "endpoint_mutation_is_migration": False,
            "migration_like_transition_is_successful_migration": False,
            "migrant_follow_through_supported": False,
            "provider_flow_supported": False,
            "network_population_supported": False,
        },
        "bundle": {
            "schema": composition.SCHEMA_BUNDLE,
            "facts": facts,
        },
    }


@contextmanager
def suppress_core_dumps() -> Iterator[bool]:
    """Temporarily disable process core dumps while raw PLC rows are live."""
    try:
        import resource
        old = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, old[1]))
    except (ImportError, OSError, ValueError):
        yield False
        return
    try:
        yield True
    finally:
        resource.setrlimit(resource.RLIMIT_CORE, old)


@contextmanager
def controlled_termination_signals() -> Iterator[bool]:
    """Turn SIGINT/SIGTERM into refusals so reducer ``finally`` blocks run."""
    watched = ((signal.SIGINT, "SIGINT", 130),
               (signal.SIGTERM, "SIGTERM", 143))
    old_handlers: dict[int, Any] = {}

    def interrupt(signum, _frame):
        for candidate, name, exit_code in watched:
            if signum == candidate:
                raise PLCReductionInterrupted(name, exit_code)
        raise PLCReductionInterrupted("SIGNAL", 128)

    try:
        for signum, _name, _exit_code in watched:
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
    except (OSError, RuntimeError, ValueError):
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        yield False
        return
    try:
        yield True
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def reduce_path(path: str | Path, *, acquired_at: dt.datetime,
                window_seconds: int = DEFAULT_WINDOW_SECONDS,
                disclosure_count: int = DEFAULT_DISCLOSURE_COUNT,
                core_dump_suppressed: bool = False) -> dict:
    """Read source rows without putting raw content in an error message."""
    try:
        with Path(path).open("rb") as source:
            def bounded_lines() -> Iterator[str]:
                line_number = 0
                while True:
                    payload = source.readline(MAX_LINE_BYTES + 2)
                    if not payload:
                        return
                    line_number += 1
                    if len(payload) > MAX_LINE_BYTES:
                        raise PLCReductionRefused(
                            "OPERATION_TOO_LARGE", f"input.line[{line_number}]",
                            "PLC export row exceeds the installed input bound")
                    try:
                        yield payload.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise PLCReductionRefused(
                            "INVALID_INPUT_ENCODING", f"input.line[{line_number}]",
                            "PLC export row is not valid UTF-8") from exc
            return reduce_jsonl(
                bounded_lines(), acquired_at=acquired_at,
                window_seconds=window_seconds,
                disclosure_count=disclosure_count,
                core_dump_suppressed=core_dump_suppressed)
    except OSError as exc:
        raise PLCReductionRefused(
            "INPUT_UNREADABLE", "input",
            f"PLC input could not be read ({type(exc).__name__})") from exc
