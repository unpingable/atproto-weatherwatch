"""PLC-REDUCTION-WITHOUT-IDENTITY-ESCAPE adversarial tests."""

from __future__ import annotations

import datetime as dt
import json
import signal
from contextlib import contextmanager
from pathlib import Path

import pytest

from weatherwatch import cli, composition, plc_reducer


UTC = dt.timezone.utc
ACQUIRED = dt.datetime(2026, 8, 24, tzinfo=UTC)
WEEK_START = dt.datetime(2026, 8, 3, tzinfo=UTC)
BASE32 = "abcdefghijklmnopqrstuvwxyz234567"


def plc_did(number: int) -> str:
    chars = []
    for _ in range(24):
        chars.append(BASE32[number % 32])
        number //= 32
    return "did:plc:" + "".join(reversed(chars))


def operation(endpoint: str, *, prev: str | None = None) -> dict:
    return {
        "type": "plc_operation",
        "rotationKeys": ["did:key:zRawRotationSecret"],
        "verificationMethods": {"atproto": "did:key:zRawSigningSecret"},
        "alsoKnownAs": ["at://raw-handle.example"],
        "services": {
            "atproto_pds": {
                "type": "AtprotoPersonalDataServer",
                "endpoint": endpoint,
            }
        },
        "prev": prev,
        "sig": "raw-signature-secret",
    }


def row(seq: int, did: str, when: dt.datetime, endpoint: str, *,
        prev: str | None = None) -> dict:
    return {
        "type": "sequenced_op",
        "operation": operation(endpoint, prev=prev),
        "did": did,
        "cid": f"bafy-raw-cid-secret-{seq}",
        "createdAt": plc_reducer.iso(when),
        "seq": seq,
    }


def tombstone_row(seq: int, did: str, when: dt.datetime) -> dict:
    return {
        "type": "sequenced_op",
        "operation": {
            "type": "plc_tombstone",
            "prev": f"bafy-raw-tombstone-prev-{seq}",
            "sig": "raw-tombstone-signature",
        },
        "did": did,
        "cid": f"bafy-raw-tombstone-cid-{seq}",
        "createdAt": plc_reducer.iso(when),
        "seq": seq,
    }


def lines(rows: list[dict]) -> list[str]:
    return [json.dumps(item) for item in rows]


def paired_rows(*, transitions: set[int] | None = None,
                start: dt.datetime = WEEK_START,
                from_endpoint: str = "https://from-provider.invalid",
                to_endpoint: str = "https://to-provider.invalid") -> list[dict]:
    transitions = transitions or set()
    rows = []
    seq = 1
    for index in range(10):
        did = plc_did(index + 1)
        first = from_endpoint
        second = to_endpoint if index in transitions else from_endpoint
        rows.append(row(seq, did, start + dt.timedelta(minutes=index), first))
        seq += 1
        rows.append(row(seq, did, start + dt.timedelta(hours=1, minutes=index),
                        second, prev=f"bafy-prev-secret-{index}"))
        seq += 1
    return rows


def transition_lattice_rows(*, migrations: int, other_mutations: int,
                            unchanged_updates: int = 0,
                            start: dt.datetime = WEEK_START) -> list[dict]:
    """Rows whose published subset lattice is known exactly."""
    rows = []
    seq = 1
    total = migrations + other_mutations + unchanged_updates
    for index in range(total):
        did = plc_did(index + 1)
        if index < migrations:
            first, second = ("https://from-provider.invalid",
                             "https://to-provider.invalid")
        elif index < migrations + other_mutations:
            # Both endpoint states are syntactically invalid due to their path,
            # so this is an endpoint mutation but not migration-like.
            first, second = ("https://same-provider.invalid/old",
                             "https://same-provider.invalid/new")
        else:
            first = second = "https://unchanged-provider.invalid"
        rows.append(row(seq, did, start + dt.timedelta(minutes=index), first))
        seq += 1
        rows.append(row(seq, did, start + dt.timedelta(hours=1, minutes=index),
                        second, prev=f"bafy-prev-{index}"))
        seq += 1
    return rows


def creation_rows(count: int, *, start: dt.datetime = WEEK_START) -> list[dict]:
    return [row(index + 1, plc_did(index + 1),
                start + dt.timedelta(minutes=index),
                "https://provider.invalid")
            for index in range(count)]


def reduce(rows: list[dict], *, acquired_at: dt.datetime = ACQUIRED,
           threshold: int = 10) -> dict:
    return plc_reducer.reduce_jsonl(
        lines(rows), acquired_at=acquired_at,
        disclosure_count=threshold, core_dump_suppressed=True)


def fact_for(document: dict, measurement: str, index: int = 0) -> dict:
    facts = [fact for fact in document["bundle"]["facts"]
             if fact["measurement"] == measurement]
    return facts[index]


def test_real_sequenced_export_reduces_to_composable_identity_free_facts():
    rows = paired_rows(transitions=set(range(10)))
    document = reduce(rows)
    persisted = json.dumps(document, sort_keys=True)

    assert document["schema"] == plc_reducer.SCHEMA_REDUCTION
    assert document["reduction_authority"] is False
    assert document["publication_authority"] is False
    assert document["custody"]["cross_row_identity_state"] == "memory_only_and_cleared"
    assert document["custody"]["core_dump_suppressed_during_reduction"] is True
    assert document["source_envelope"]["directory_completeness_claimed"] is False
    assert document["semantic_limits"]["migration_like_transition_is_successful_migration"] is False

    for item in rows:
        assert item["did"] not in persisted
        assert item["cid"] not in persisted
        endpoint = item["operation"]["services"]["atproto_pds"]["endpoint"]
        assert endpoint not in persisted
    assert "raw-handle.example" not in persisted
    assert "raw-signature-secret" not in persisted
    assert all(fact["dimensions"] == {} for fact in document["bundle"]["facts"])

    _, facts = composition.validate_bundle(document["bundle"])
    assert len(facts) == len(plc_reducer.MEASUREMENTS)
    migration = fact_for(document, "plc.directory.migration_like_transitions")
    assert migration["value"] == 10
    assert migration["state"] == "DEGRADED"


def test_zero_and_singleton_transition_windows_are_indistinguishable():
    zero = reduce(paired_rows())
    singleton = reduce(paired_rows(transitions={0}))
    for measurement in plc_reducer.TRANSITION_MEASUREMENTS:
        assert fact_for(zero, measurement) == fact_for(singleton, measurement)
        assert fact_for(singleton, measurement)["state"] == "UNKNOWN"
        assert fact_for(singleton, measurement)["value"] is None


@pytest.mark.parametrize(("count", "expected"), [
    (9, None), (10, 10), (11, 11),
])
def test_per_fact_threshold_edges_are_exact(count, expected):
    document = reduce(transition_lattice_rows(
        migrations=count, other_mutations=0))
    migration = fact_for(document, "plc.directory.migration_like_transitions")
    assert migration["value"] == expected
    assert migration["state"] == ("UNKNOWN" if expected is None else "DEGRADED")


def test_disclosed_transition_facts_can_reveal_a_small_complement():
    document = reduce(transition_lattice_rows(
        migrations=10, other_mutations=2))
    mutations = fact_for(document, "plc.directory.endpoint_mutations")["value"]
    migrations = fact_for(
        document, "plc.directory.migration_like_transitions")["value"]
    assert mutations == 12
    assert migrations == 10
    assert mutations - migrations == 2
    assert document["disclosure_claim"]["compositional_non_disclosure_claimed"] is False
    assert document["disclosure_claim"]["derived_small_aggregate_counts_possible"] is True


def test_operation_creation_complement_can_reveal_zero_or_singleton():
    ten_creations = creation_rows(10)
    zero = reduce(ten_creations)
    one = reduce(ten_creations + [row(
        11, plc_did(1), WEEK_START + dt.timedelta(hours=2),
        "https://provider.invalid", prev="bafy-prev-singleton")])
    zero_difference = (
        fact_for(zero, "plc.directory.operations")["value"]
        - fact_for(zero, "plc.directory.creations")["value"])
    one_difference = (
        fact_for(one, "plc.directory.operations")["value"]
        - fact_for(one, "plc.directory.creations")["value"])
    assert zero_difference == 0
    assert one_difference == 1


def test_operation_tombstone_complement_can_reveal_singleton():
    rows = [tombstone_row(index + 1, plc_did(index + 1),
                          WEEK_START + dt.timedelta(minutes=index))
            for index in range(10)]
    rows.append(row(11, plc_did(20), WEEK_START + dt.timedelta(hours=2),
                    "https://provider.invalid"))
    document = reduce(rows)
    operations = fact_for(document, "plc.directory.operations")["value"]
    tombstones = fact_for(document, "plc.directory.tombstones")["value"]
    assert operations == 11
    assert tombstones == 10
    assert operations - tombstones == 1


def test_combined_subset_remainder_can_reveal_one_unchanged_update():
    rows = transition_lattice_rows(
        migrations=10, other_mutations=0, unchanged_updates=1)
    rows.extend(tombstone_row(
        len(rows) + index + 1, plc_did(100 + index),
        WEEK_START + dt.timedelta(hours=3, minutes=index))
                for index in range(10))
    rows.sort(key=lambda item: item["seq"])
    document = reduce(rows)
    values = {measurement: fact_for(document, measurement)["value"]
              for measurement in plc_reducer.MEASUREMENTS}
    remainder = (values["plc.directory.operations"]
                 - values["plc.directory.creations"]
                 - values["plc.directory.tombstones"]
                 - values["plc.directory.endpoint_mutations"])
    assert remainder == 1


def test_cross_measurement_arithmetic_reconstructs_one_zero_one_pattern():
    rows = []
    seq = 1
    for week, complement in enumerate((1, 0, 1)):
        week_rows = transition_lattice_rows(
            migrations=10, other_mutations=complement,
            start=WEEK_START + dt.timedelta(weeks=week))
        for item in week_rows:
            item["seq"] = seq
            item["cid"] = f"bafy-weekly-secret-{seq}"
            seq += 1
        rows.extend(week_rows)
    document = reduce(rows, acquired_at=WEEK_START + dt.timedelta(weeks=4))
    mutations = [fact["value"] for fact in document["bundle"]["facts"]
                 if fact["measurement"] == "plc.directory.endpoint_mutations"]
    migrations = [fact["value"] for fact in document["bundle"]["facts"]
                  if fact["measurement"] == "plc.directory.migration_like_transitions"]
    assert [left - right for left, right in zip(mutations, migrations)] == [1, 0, 1]


def test_rare_provider_tuple_is_never_emitted_even_above_count_threshold():
    document = reduce(paired_rows(transitions=set(range(10))))
    persisted = json.dumps(document)
    assert "from-provider" not in persisted
    assert "to-provider" not in persisted
    assert document["custody"]["provider_dimensions_emitted"] is False
    migration = fact_for(document, "plc.directory.migration_like_transitions")
    assert migration["value"] == 10
    assert migration["dimensions"] == {}


def test_narrow_publication_window_is_refused_by_policy():
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_jsonl(
            lines(paired_rows()), acquired_at=ACQUIRED, window_seconds=300)
    assert raised.value.code == "UNSAFE_WINDOW_POLICY"


def test_each_transition_fact_hides_zero_one_zero_sequences_in_isolation():
    quiet_rows: list[dict] = []
    sparse_rows: list[dict] = []
    seq = 1
    for week in range(3):
        start = WEEK_START + dt.timedelta(weeks=week)
        quiet = paired_rows(start=start)
        sparse = paired_rows(start=start, transitions={0} if week != 1 else set())
        for left, right in zip(quiet, sparse):
            left["seq"] = right["seq"] = seq
            left["cid"] = right["cid"] = f"bafy-secret-{seq}"
            seq += 1
        quiet_rows.extend(quiet)
        sparse_rows.extend(sparse)
    acquired = WEEK_START + dt.timedelta(weeks=4)
    quiet = reduce(quiet_rows, acquired_at=acquired)
    sparse = reduce(sparse_rows, acquired_at=acquired)
    for measurement in plc_reducer.TRANSITION_MEASUREMENTS:
        q = [fact for fact in quiet["bundle"]["facts"]
             if fact["measurement"] == measurement]
        s = [fact for fact in sparse["bundle"]["facts"]
             if fact["measurement"] == measurement]
        assert q == s
        assert [fact["value"] for fact in s] == [None, None, None]


def test_endpoint_mutation_is_distinct_from_migration_like_transition():
    rows = paired_rows(
        transitions=set(range(10)),
        from_endpoint="https://same-provider.invalid/old",
        to_endpoint="https://same-provider.invalid/new")
    document = reduce(rows)
    mutation = fact_for(document, "plc.directory.endpoint_mutations")
    migration = fact_for(document, "plc.directory.migration_like_transitions")
    assert mutation["value"] == 10
    assert migration["value"] is None
    assert migration["state"] == "UNKNOWN"
    assert document["semantic_limits"]["endpoint_mutation_is_migration"] is False


def test_partial_history_never_becomes_complete_migration_coverage():
    rows = [row(index + 100, plc_did(index + 1),
                WEEK_START + dt.timedelta(minutes=index),
                "https://new-provider.invalid", prev=f"bafy-missing-{index}")
            for index in range(11)]
    document = reduce(rows)
    assert document["source_envelope"]["unresolved_update_predecessors"] == 11
    assert document["source_envelope"]["directory_completeness_claimed"] is False
    assert fact_for(document, "plc.directory.operations")["value"] == 11
    assert fact_for(document, "plc.directory.migration_like_transitions")["value"] is None
    assert document["source_envelope"]["population_denominator_claimed"] is False


def test_backfill_revises_event_window_without_backdating_knowledge():
    original_rows = [row(index + 1, plc_did(index + 1),
                         WEEK_START + dt.timedelta(minutes=index),
                         "https://provider.invalid")
                     for index in range(10)]
    first_acquired = dt.datetime(2026, 8, 17, tzinfo=UTC)
    second_acquired = dt.datetime(2026, 8, 24, tzinfo=UTC)
    first = reduce(original_rows, acquired_at=first_acquired)
    revised_rows = original_rows + [row(
        11, plc_did(20), WEEK_START + dt.timedelta(days=1),
        "https://provider.invalid")]
    second = reduce(revised_rows, acquired_at=second_acquired)
    before = fact_for(first, "plc.directory.operations")
    after = fact_for(second, "plc.directory.operations")
    assert before["window"] == after["window"]
    assert before["value"] == 10
    assert after["value"] == 11
    assert before["acquired_at"] == "2026-08-17T00:00:00Z"
    assert after["acquired_at"] == "2026-08-24T00:00:00Z"


def test_repeated_acquisition_differencing_can_reveal_small_backfills():
    acquired = [dt.datetime(2026, 8, day, tzinfo=UTC)
                for day in (17, 24, 31)]
    releases = [reduce(creation_rows(count), acquired_at=when)
                for count, when in zip((10, 11, 12), acquired)]
    values = [fact_for(item, "plc.directory.operations")["value"]
              for item in releases]
    acquisition_times = [fact_for(item, "plc.directory.operations")["acquired_at"]
                         for item in releases]
    assert values == [10, 11, 12]
    assert [right - left for left, right in zip(values, values[1:])] == [1, 1]
    assert acquisition_times == ["2026-08-17T00:00:00Z",
                                 "2026-08-24T00:00:00Z",
                                 "2026-08-31T00:00:00Z"]
    assert releases[-1]["disclosure_claim"]["revision_differencing_protected"] is False


@pytest.mark.parametrize(("before", "after", "before_value", "after_value"), [
    (9, 10, None, 10),
    (10, 11, 10, 11),
    (11, 10, 11, 10),
    (10, 9, 10, None),
])
def test_repeated_acquisition_threshold_crossings_are_characterized(
        before, after, before_value, after_value):
    first = reduce(creation_rows(before),
                   acquired_at=dt.datetime(2026, 8, 17, tzinfo=UTC))
    second = reduce(creation_rows(after),
                    acquired_at=dt.datetime(2026, 8, 24, tzinfo=UTC))
    assert fact_for(first, "plc.directory.operations")["value"] == before_value
    assert fact_for(second, "plc.directory.operations")["value"] == after_value
    # UNKNOWN says only 0..9; crossing it reveals a range, while two disclosed
    # releases reveal their exact arithmetic delta.
    if before_value is not None and after_value is not None:
        assert after_value - before_value == after - before


def test_source_clock_after_acquisition_refuses_without_echo():
    canary = plc_did(999)
    future = ACQUIRED + dt.timedelta(seconds=1)
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        reduce([row(1, canary, future, "https://secret-provider.invalid")])
    refusal = json.dumps(raised.value.as_dict())
    assert raised.value.code == "EVENT_TIME_AFTER_ACQUISITION"
    assert canary not in refusal
    assert "secret-provider" not in refusal


def test_malformed_raw_input_never_escapes_in_refusal_or_logs(caplog):
    canary = plc_did(0)
    malformed = '{"did":"' + canary + '","endpoint":"https://secret.invalid"'
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_jsonl([malformed], acquired_at=ACQUIRED)
    refusal = json.dumps(raised.value.as_dict())
    assert raised.value.code == "MALFORMED_JSON"
    assert canary not in refusal
    assert "secret.invalid" not in refusal
    assert canary not in caplog.text


def test_legacy_timestamp_export_shape_is_refused():
    legacy = row(1, plc_did(1), WEEK_START, "https://provider.invalid")
    legacy.pop("type")
    legacy.pop("seq")
    legacy["nullified"] = False
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_jsonl(lines([legacy]), acquired_at=ACQUIRED)
    assert raised.value.code == "UNSUPPORTED_EXPORT_SHAPE"


def test_empty_export_is_unknown_not_green():
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_jsonl([], acquired_at=ACQUIRED)
    assert raised.value.code == "EMPTY_EXPORT"


def test_disclosure_threshold_cannot_be_weakened():
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        reduce(paired_rows(), threshold=9)
    assert raised.value.code == "UNSAFE_DISCLOSURE_POLICY"


def test_oversized_line_refuses_without_echo(monkeypatch):
    monkeypatch.setattr(plc_reducer, "MAX_LINE_BYTES", 64)
    canary = plc_did(0)
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_jsonl(
            [json.dumps({"canary": canary, "padding": "x" * 100})],
            acquired_at=ACQUIRED)
    assert raised.value.code == "OPERATION_TOO_LARGE"
    assert canary not in json.dumps(raised.value.as_dict())


def test_supported_file_reader_bounds_line_before_decode(tmp_path, monkeypatch):
    monkeypatch.setattr(plc_reducer, "MAX_LINE_BYTES", 64)
    source = tmp_path / "oversized.jsonl"
    canary = plc_did(0).encode()
    source.write_bytes(canary + b"x" * 100 + b"\n")
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_path(source, acquired_at=ACQUIRED)
    assert raised.value.code == "OPERATION_TOO_LARGE"
    assert canary.decode() not in json.dumps(raised.value.as_dict())


def test_supported_file_reader_refuses_invalid_utf8_without_echo(tmp_path):
    source = tmp_path / "invalid-utf8.jsonl"
    source.write_bytes(b'{"did":"' + plc_did(0).encode() + b'"}\xff\n')
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_path(source, acquired_at=ACQUIRED)
    assert raised.value.code == "INVALID_INPUT_ENCODING"
    assert "did:plc" not in json.dumps(raised.value.as_dict())


def test_oversized_endpoint_refuses_without_echo(monkeypatch):
    monkeypatch.setattr(plc_reducer, "MAX_ENDPOINT_BYTES", 32)
    canary = "secret-endpoint-canary"
    endpoint = "https://" + canary + ".invalid/" + "x" * 100
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        reduce([row(1, plc_did(1), WEEK_START, endpoint)])
    assert raised.value.code == "ENDPOINT_TOO_LARGE"
    assert canary not in json.dumps(raised.value.as_dict())


def test_operation_count_bound_refuses_before_unbounded_work(monkeypatch):
    monkeypatch.setattr(plc_reducer, "MAX_OPERATIONS", 2)
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        reduce(creation_rows(3))
    assert raised.value.code == "OPERATION_LIMIT_EXCEEDED"


def test_distinct_identity_history_bound_refuses(monkeypatch):
    monkeypatch.setattr(plc_reducer, "MAX_DISTINCT_IDENTITIES", 2)
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        reduce(creation_rows(3))
    assert raised.value.code == "IDENTITY_HISTORY_LIMIT_EXCEEDED"


def test_tracked_window_bound_refuses(monkeypatch):
    monkeypatch.setattr(plc_reducer, "MAX_TRACKED_WINDOWS", 2)
    rows = [row(index + 1, plc_did(index + 1),
                WEEK_START + dt.timedelta(weeks=index),
                "https://provider.invalid")
            for index in range(3)]
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        reduce(rows, acquired_at=WEEK_START + dt.timedelta(weeks=4))
    assert raised.value.code == "WINDOW_LIMIT_EXCEEDED"


def test_pathological_json_nesting_is_a_non_echoing_refusal():
    hostile = "[" * 1200 + "]" * 1200
    with pytest.raises(plc_reducer.PLCReductionRefused) as raised:
        plc_reducer.reduce_jsonl([hostile], acquired_at=ACQUIRED)
    assert raised.value.code == "JSON_NESTING_LIMIT_EXCEEDED"
    assert len(json.dumps(raised.value.as_dict())) < 500


@pytest.mark.parametrize(("signum", "exit_code", "code"), [
    (signal.SIGINT, 130, "INTERRUPTED_SIGINT"),
    (signal.SIGTERM, 143, "INTERRUPTED_SIGTERM"),
])
def test_cli_turns_termination_signals_into_custodied_refusals(
        tmp_path, capsys, monkeypatch, signum, exit_code, code):
    source = tmp_path / "raw-plc.jsonl"
    source.write_text("{}\n")
    original = signal.getsignal(signum)

    def interrupt(*args, **kwargs):
        signal.raise_signal(signum)

    monkeypatch.setattr(plc_reducer, "reduce_path", interrupt)
    rc = cli.main([
        "plc-reduce", "--input", str(source),
        "--acquired-at", "2026-08-24T00:00:00Z"])
    output = capsys.readouterr().out
    assert rc == exit_code
    assert code in output
    assert signal.getsignal(signum) is original


@pytest.mark.parametrize("exit_kind", ["success", "refusal", "exception"])
def test_transient_identity_history_is_cleared_on_every_exit(
        monkeypatch, exit_kind):
    class TrackedHistory(dict):
        cleared = False

        def clear(self):
            self.cleared = True
            super().clear()

    history = TrackedHistory()
    monkeypatch.setattr(plc_reducer, "_new_transient_history", lambda: history)
    source = lines(creation_rows(1))
    if exit_kind == "refusal":
        monkeypatch.setattr(plc_reducer, "MAX_OPERATIONS", 1)
        source += lines(creation_rows(1))
    elif exit_kind == "exception":
        first_line = source[0]
        def broken_source():
            yield first_line
            raise RuntimeError("synthetic iterator failure")
        source = broken_source()
    try:
        plc_reducer.reduce_jsonl(source, acquired_at=ACQUIRED)
    except (plc_reducer.PLCReductionRefused, RuntimeError):
        pass
    assert history.cleared is True
    assert history == {}


def test_cli_disables_core_dump_and_can_emit_composition_bundle(
        tmp_path, capsys):
    source = tmp_path / "raw-plc.jsonl"
    source.write_text("\n".join(lines(paired_rows(transitions=set(range(10))))) + "\n")
    rc = cli.main([
        "plc-reduce", "--input", str(source),
        "--acquired-at", "2026-08-24T00:00:00Z"])
    receipt = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert receipt["custody"]["core_dump_suppressed_during_reduction"] is True

    rc = cli.main([
        "plc-reduce", "--input", str(source),
        "--acquired-at", "2026-08-24T00:00:00Z", "--bundle-only"])
    output = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert output["schema"] == composition.SCHEMA_BUNDLE
    composition.validate_bundle(output)
    assert "raw-handle.example" not in json.dumps(output)


def test_cli_unexpected_failure_does_not_echo_raw_exception(
        tmp_path, capsys, monkeypatch):
    source = tmp_path / "raw-plc.jsonl"
    source.write_text("{}\n")
    canary = plc_did(0) + " https://secret.invalid"
    custody = {"active": False}

    @contextmanager
    def guarded():
        custody["active"] = True
        try:
            yield True
        finally:
            custody["active"] = False

    def crash(*args, **kwargs):
        assert custody["active"] is True
        raise RuntimeError(canary)

    monkeypatch.setattr(plc_reducer, "suppress_core_dumps", guarded)
    monkeypatch.setattr(plc_reducer, "reduce_path", crash)
    rc = cli.main([
        "plc-reduce", "--input", str(source),
        "--acquired-at", "2026-08-24T00:00:00Z"])
    output = capsys.readouterr().out
    assert rc == 2
    assert custody["active"] is False
    assert "REDUCER_FAILED" in output
    assert canary not in output


def test_persisted_reducer_state_has_no_supported_identity_linkage(tmp_path):
    rows = paired_rows(transitions=set(range(10)))
    document = reduce(rows)
    persisted_path = tmp_path / "reduction.json"
    persisted_path.write_text(json.dumps(document))
    persisted = json.loads(persisted_path.read_text())

    # The persisted artifact has only aggregate windows and empty dimensions;
    # there is no row, dimension, key, hash, endpoint, or cohort on which a
    # supported operation could recover or link one PLC identity.
    assert all(fact["dimensions"] == {} for fact in persisted["bundle"]["facts"])
    serialized = json.dumps(persisted)
    for item in rows:
        assert item["did"] not in serialized
        assert item["cid"] not in serialized
    for forbidden in ("from-provider.invalid", "to-provider.invalid",
                      "raw-handle.example", "raw-signature-secret"):
        assert forbidden not in serialized


def test_plc_reduction_schemas_and_contract_are_versioned():
    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / ".ops/plc-reduction.schema.json").read_text())
    refusal = json.loads(
        (root / ".ops/plc-reduction-refusal.schema.json").read_text())
    contracts = json.loads(
        (root / ".ops/composition-contracts.json").read_text())
    source_contract = json.loads(
        (root / ".ops/plc-source-contract.json").read_text())
    source_schema = json.loads(
        (root / ".ops/plc-source-contract.schema.json").read_text())
    plc_contract = next(item for item in contracts["contracts"]
                        if item["contract_id"] == plc_reducer.CONTRACT_ID)
    assert schema["$id"] == plc_reducer.SCHEMA_REDUCTION
    assert refusal["$id"] == plc_reducer.SCHEMA_REFUSAL
    assert source_schema["$id"] == source_contract["schema"]
    assert source_contract["revision"] == plc_reducer.SOURCE_REVISION
    assert source_contract["upstream_repository"] == plc_reducer.SOURCE_REPOSITORY
    assert source_contract["specification"] == {
        "path": plc_reducer.SOURCE_SPEC_PATH,
        "section": plc_reducer.SOURCE_SPEC_SECTION,
        "version": plc_reducer.SOURCE_SPEC_VERSION,
        "sha256": plc_reducer.SOURCE_SPEC_SHA256,
    }
    assert source_contract["future_revision_equivalence_automatic"] is False
    assert set(plc_contract["measurements"]) == set(plc_reducer.MEASUREMENTS)
    assert plc_contract["allowed_dimensions"] == {}
