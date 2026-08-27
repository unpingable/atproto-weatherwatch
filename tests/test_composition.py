"""TEMPORAL-COMPOSITION-WITHOUT-IDENTITY-LAUNDERING adversarial tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from weatherwatch import cli, composition


START = "2026-08-27T12:00:00Z"
END = "2026-08-27T13:00:00Z"


def contract(contract_id, producer_id, measurements, dimensions=None):
    return {
        "contract_id": contract_id,
        "producer_id": producer_id,
        "vantage_profile": f"{contract_id}.vantage",
        "coverage_profile": f"{contract_id}.coverage",
        "measurements": {
            measurement: {"semantic_profile": f"{measurement}.v1", "unit": unit}
            for measurement, unit in measurements.items()
        },
        "allowed_dimensions": dimensions or {},
    }


PRODUCTION = contract(
    "weatherwatch.jetstream.production.v1",
    "weatherwatch.reducer.jetstream.v1",
    {"atproto.public_record_writes": "count",
     "atproto.block_record_writes": "count",
     "atproto.active_actor_hll": "approximate_count"},
    {"record_family": ["app_bsky", "other"]},
)
SEARCH = contract(
    "external.trends.search.v1",
    "external.reducer.trends.v1",
    {"external.normalized_search_interest": "normalized_index"},
)
LABELS = contract(
    "labelwatch.issuance.v1",
    "labelwatch.reducer.issuance.v1",
    {"labelwatch.label_issuance": "count"},
)
PLC = contract(
    "plc.directory.operations.v1",
    "plc.reducer.operations.v1",
    {"plc.directory.operations": "count",
     "plc.directory.population": "count"},
)
REGISTRY = {
    "schema": composition.SCHEMA_CONTRACTS,
    "contracts": [PRODUCTION, SEARCH, LABELS, PLC],
}


def fact(contract_id, measurement, value, unit="count", *, start=START,
         end=END, acquired_at="2026-08-27T18:00:00Z", state="PRESENT",
         coverage=1.0, dimensions=None):
    return {
        "contract_id": contract_id,
        "measurement": measurement,
        "window": {"start": start, "end": end},
        "acquired_at": acquired_at,
        "state": state,
        "coverage_fraction": coverage,
        "value": value,
        "unit": unit,
        "dimensions": dimensions or {},
    }


def bundle(contracts, facts):
    # Contracts are intentionally ignored here: facts cannot bless their own
    # source contract. They are admitted from the separate repo registry.
    return {"schema": composition.SCHEMA_BUNDLE,
            "facts": copy.deepcopy(facts)}


def compose_doc(document, rule_id, registry=None):
    return composition.compose(
        document, rule_id, copy.deepcopy(registry or REGISTRY))


def overlap_bundle(*, left_state="PRESENT", right_state="PRESENT",
                   left_coverage=1.0, right_coverage=1.0):
    return bundle([PRODUCTION, LABELS], [
        fact(PRODUCTION["contract_id"], "atproto.block_record_writes", 90,
             state=left_state, coverage=left_coverage),
        fact(LABELS["contract_id"], "labelwatch.label_issuance", 12,
             state=right_state, coverage=right_coverage),
    ])


def test_clock_only_overlap_produces_bounded_candidate_claim():
    output = compose_doc(
        overlap_bundle(), "weatherwatch.rule.block-label-proximity.v1")
    assert output["disposition"] == "COMPOSED"
    assert output["state"] == "PRESENT"
    assert output["join_basis"] == "bounded_temporal_overlap_only"
    assert output["composition_authority"] is False
    assert output["source_admission_performed"] is False
    claim = output["claims"][0]
    assert claim["relation"]["kind"] == "simultaneous"
    assert claim["relation"]["overlap"] == {"start": START, "end": END}
    assert "temporal proximity only" in claim["statement"]
    assert "moderation caused blocking" in output["forbidden_interpretations"]


def test_dimensions_are_never_used_as_join_keys():
    doc = overlap_bundle()
    doc["facts"][0]["dimensions"] = {"record_family": "app_bsky"}
    # The other source has no dimension at all. Temporal overlap still is the
    # only join basis; no equality or crosswalk is attempted.
    output = compose_doc(
        doc, "weatherwatch.rule.block-label-proximity.v1")
    assert output["claims"][0]["relation"]["kind"] == "simultaneous"


@pytest.mark.parametrize(("name", "value", "code"), [
    ("actor", "cohort_1", "IDENTITY_DIMENSION"),
    ("record_family", "did:plc:secret", "UNBOUNDED_DIMENSION"),
    ("record_family", "0123456789abcdef01234567", "UNBOUNDED_DIMENSION"),
])
def test_identity_and_pseudonym_smuggling_through_dimensions_refuses(
    name, value, code):
    doc = overlap_bundle()
    doc["facts"][0]["dimensions"] = {name: value}
    registry = copy.deepcopy(REGISTRY)
    registry["contracts"][0]["allowed_dimensions"] = {name: [value]}
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1", registry)
    assert raised.value.code == code
    assert value not in json.dumps(raised.value.as_dict())
    assert raised.value.as_dict()["rejected_value_echoed"] is False


def test_free_form_label_smuggling_refuses_closed_fact_shape():
    doc = overlap_bundle()
    doc["facts"][0]["label"] = "did:plc:secret"
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "UNDECLARED_FIELD"
    assert "secret" not in json.dumps(raised.value.as_dict())


def test_fact_bundle_cannot_bless_its_own_source_contract():
    doc = overlap_bundle()
    doc["contracts"] = [copy.deepcopy(PRODUCTION)]
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "UNDECLARED_FIELD"


def test_contract_cannot_invent_a_pseudonymous_dimension_profile():
    registry = copy.deepcopy(REGISTRY)
    registry["contracts"][0]["allowed_dimensions"] = {"cohort": ["alice"]}
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(overlap_bundle(),
                    "weatherwatch.rule.block-label-proximity.v1", registry)
    assert raised.value.code == "DIMENSION_PROFILE_NOT_INSTALLED"


def test_missing_source_is_unsatisfied_not_zero_or_omitted():
    doc = bundle([PRODUCTION], [
        fact(PRODUCTION["contract_id"], "atproto.block_record_writes", 0)])
    output = compose_doc(
        doc, "weatherwatch.rule.block-label-proximity.v1")
    assert output["disposition"] == "UNSATISFIED"
    assert output["state"] == "ABSENT"
    assert output["claims"] == []
    assert "absent" in output["reason"]


def test_unknown_source_fact_is_not_a_quiet_measurement():
    doc = overlap_bundle(right_state="UNKNOWN", right_coverage=None)
    doc["facts"][1]["value"] = None
    output = compose_doc(
        doc, "weatherwatch.rule.block-label-proximity.v1")
    assert output["disposition"] == "UNSATISFIED"
    assert output["state"] == "UNKNOWN"
    assert output["claims"] == []


def test_stale_value_cannot_silently_refresh_present_qualification():
    doc = overlap_bundle(right_state="STALE", right_coverage=None)
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "HISTORICAL_VALUE_AS_CURRENT"


def test_stale_null_fact_propagates_stale():
    doc = overlap_bundle(right_state="STALE", right_coverage=None)
    doc["facts"][1]["value"] = None
    output = compose_doc(
        doc, "weatherwatch.rule.block-label-proximity.v1")
    assert output["disposition"] == "UNSATISFIED"
    assert output["state"] == "STALE"


def test_acquisition_time_is_required_and_preserved_in_claim_inputs():
    doc = overlap_bundle()
    output = compose_doc(
        doc, "weatherwatch.rule.block-label-proximity.v1")
    inputs = output["claims"][0]["inputs"]
    assert {item["acquired_at"] for item in inputs} == {
        "2026-08-27T18:00:00Z"}
    del doc["facts"][0]["acquired_at"]
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "MISSING_FIELD"


def test_event_window_cannot_be_known_before_acquisition():
    doc = overlap_bundle()
    doc["facts"][0]["acquired_at"] = "2026-08-27T11:59:59Z"
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "ACQUISITION_PRECEDES_EVENT_WINDOW"


def test_two_partial_envelopes_do_not_union_into_complete_coverage():
    output = compose_doc(
        overlap_bundle(left_state="DEGRADED", right_state="DEGRADED",
                       left_coverage=.7, right_coverage=.6),
        "weatherwatch.rule.block-label-proximity.v1")
    claim = output["claims"][0]
    assert output["state"] == "DEGRADED"
    assert claim["coverage"]["fraction"] == pytest.approx(.6)
    assert "never a union" in claim["coverage"]["basis"]


def test_non_overlapping_sources_do_not_become_a_zero_relation():
    doc = overlap_bundle()
    doc["facts"][1]["window"] = {
        "start": "2026-08-27T15:00:00Z", "end": "2026-08-27T16:00:00Z"}
    output = compose_doc(
        doc, "weatherwatch.rule.block-label-proximity.v1")
    assert output["disposition"] == "UNSATISFIED"
    assert output["state"] == "UNKNOWN"
    assert output["claims"] == []


def test_arbitrary_arithmetic_and_uninstalled_claims_refuse():
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(overlap_bundle(), "writes_times_trends_is_engagement")
    assert raised.value.code == "RULE_NOT_INSTALLED"


def test_two_jetstream_observers_cannot_be_summed_into_network_total():
    doc = bundle([PRODUCTION], [
        fact(PRODUCTION["contract_id"], "atproto.public_record_writes", 100,
             dimensions={"record_family": "app_bsky"}),
    ])
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.jetstream-network-total.v1")
    assert raised.value.code == "RULE_NOT_INSTALLED"


def test_hll_and_plc_population_cannot_become_dau_percentage():
    doc = bundle([PRODUCTION, PLC], [
        fact(PRODUCTION["contract_id"], "atproto.active_actor_hll", 90,
             unit="approximate_count"),
        fact(PLC["contract_id"], "plc.directory.population", 1000),
    ])
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.dau-percentage.v1")
    assert raised.value.code == "RULE_NOT_INSTALLED"


def test_semantic_unit_mismatch_refuses_before_composition():
    doc = overlap_bundle()
    doc["facts"][1]["unit"] = "percent"
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "SEMANTIC_MISMATCH"


def correlation_bundle(xs=(10, 20, 30), ys=(1, 2, 3)):
    facts = []
    for index, (x, y) in enumerate(zip(xs, ys)):
        start = f"2026-08-27T{12 + index:02d}:00:00Z"
        end = f"2026-08-27T{13 + index:02d}:00:00Z"
        facts.extend([
            fact(PRODUCTION["contract_id"], "atproto.public_record_writes", x,
                 start=start, end=end),
            fact(SEARCH["contract_id"], "external.normalized_search_interest", y,
                 unit="normalized_index", start=start, end=end),
        ])
    return bundle([PRODUCTION, SEARCH], facts)


def test_explicit_correlation_rule_requires_exactly_aligned_windows():
    output = compose_doc(
        correlation_bundle(),
        "weatherwatch.rule.production-search-association.v1")
    claim = output["claims"][0]
    assert claim["relation"]["coefficient"] == pytest.approx(1.0)
    assert claim["relation"]["aligned_window_count"] == 3
    assert "attention" in output["forbidden_interpretations"]
    assert "engagement" in output["forbidden_interpretations"]
    assert "Causal direction is not established" in claim["statement"]


def test_correlation_does_not_rebin_or_guess_alignment():
    doc = correlation_bundle()
    doc["facts"][1]["window"]["start"] = "2026-08-27T12:05:00Z"
    output = compose_doc(
        doc, "weatherwatch.rule.production-search-association.v1")
    assert output["disposition"] == "UNSATISFIED"
    assert output["state"] == "UNKNOWN"
    assert "exactly aligned" in output["reason"]


def test_duplicate_fact_refuses_instead_of_double_counting():
    doc = overlap_bundle()
    doc["facts"].append(copy.deepcopy(doc["facts"][0]))
    with pytest.raises(composition.CompositionRefused) as raised:
        compose_doc(doc, "weatherwatch.rule.block-label-proximity.v1")
    assert raised.value.code == "DUPLICATE_FACT"


def test_cli_emits_machine_refusal_without_echoing_smuggled_identity(
        tmp_path, capsys):
    doc = overlap_bundle()
    doc["facts"][0]["label"] = "did:plc:do-not-echo"
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(doc))
    rc = cli.main(["compose", "--input", str(path), "--rule",
                   "weatherwatch.rule.block-label-proximity.v1"])
    output = capsys.readouterr().out
    assert rc == 2
    assert "UNDECLARED_FIELD" in output
    assert "do-not-echo" not in output


def test_cli_composes_with_the_repository_contract_registry(
        tmp_path, capsys, monkeypatch):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(overlap_bundle()))
    monkeypatch.chdir(tmp_path)  # registry discovery must not depend on cwd
    rc = cli.main(["compose", "--input", str(path), "--rule",
                   "weatherwatch.rule.block-label-proximity.v1"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["disposition"] == "COMPOSED"
    assert payload["join_basis"] == "bounded_temporal_overlap_only"
    assert payload["composition_authority"] is False


def test_cli_lists_only_installed_rules(capsys):
    assert cli.main(["compose", "--list-rules"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "weatherwatch.composition.rules/v1"
    assert {item["rule_id"] for item in payload["rules"]} == set(
        composition.RULES)


def test_declared_schemas_match_the_runtime_contract():
    root = Path(__file__).resolve().parent.parent
    bundle_schema = json.loads(
        (root / ".ops/composition-bundle.schema.json").read_text())
    output_schema = json.loads(
        (root / ".ops/composition-output.schema.json").read_text())
    contracts_schema = json.loads(
        (root / ".ops/composition-contracts.schema.json").read_text())
    registry = json.loads(
        (root / ".ops/composition-contracts.json").read_text())
    assert bundle_schema["properties"]["schema"]["const"] == composition.SCHEMA_BUNDLE
    assert output_schema["$id"] == composition.SCHEMA_OUTPUT
    assert contracts_schema["properties"]["schema"]["const"] == composition.SCHEMA_CONTRACTS
    assert registry["schema"] == composition.SCHEMA_CONTRACTS
    assert composition.validate_contracts(registry)
    assert bundle_schema["additionalProperties"] is False
    assert output_schema["properties"]["composition_authority"]["const"] is False
