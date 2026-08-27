"""Adversarial contract tests for the repo-declared visibility surface."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from weatherwatch import db, findings, visibility
from weatherwatch.publication import evaluate_candidate
from tests.conftest import SYNTH_BASE, SYNTH_ENDPOINT, build_run


NOW = dt.datetime.fromtimestamp(SYNTH_BASE + 70, tz=dt.timezone.utc)


def _path(conn) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()["file"])


def _indexed(document: dict) -> dict[str, dict]:
    indexed = {}
    for item in document["concerns"]:
        observation = item["observation"]
        indexed[item["id"]] = {
            "state": observation["local_state"],
            "summary": observation["reason"],
            "observed_at": observation["observed_at"],
            "facts": observation["facts"],
            "required": item["required"],
        }
    return indexed


def _runtime(conn, state="connected", when=NOW):
    db.set_meta(conn, visibility.RUNTIME_META_KEY, json.dumps({
        "schema": "weatherwatch.collector_runtime.v1",
        "state": state,
        "updated_at": when.isoformat().replace("+00:00", "Z"),
        "run_id": "r1",
        "endpoint": SYNTH_ENDPOINT,
        "last_frame_at": when.isoformat().replace("+00:00", "Z"),
        "last_error_kind": None,
    }, sort_keys=True))


def _active(conn, window: dict, *, cursor: int | None = None):
    build_run(conn, "r1", [window], ended_at=None,
              started_at="2026-01-01T00:00:00+00:00")
    if cursor is not None:
        db.set_meta(conn, db.cursor_key(SYNTH_ENDPOINT), str(cursor))
    _runtime(conn)


def _candidate(root: Path, *, generated: dt.datetime, newest: dt.datetime,
               recorded_state="current"):
    root.mkdir()
    (root / "index.html").write_text("<html>aggregate weather</html>")
    (root / "social.json").write_text("{}")
    (root / "summary.json").write_text(json.dumps({
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "freshness": {
            "state": recorded_state,
            "newest_observation_end": newest.isoformat().replace("+00:00", "Z"),
            "current_threshold_seconds": 660,
        },
    }))
    findings.write_artifacts(root)
    finding_dir = root / "findings" / findings.OBSERVER_DIVERGENCE_SLUG
    (finding_dir / "index.html").write_text("published aggregate finding")


def test_manifest_declares_the_exact_required_surface_without_execution_policy():
    text = Path(".ops/concerns.toml").read_text()
    blocks = re.findall(r"\[\[concerns\]\]\n(.*?)(?=\n\[\[concerns\]\]|\Z)",
                        text, re.S)
    ids = re.findall(r'^id = "([^"]+)"$', text, re.M)
    questions = re.findall(r'^question = "([^"]+)"$', text, re.M)
    profiles = re.findall(r'^profile = "([^"]+)"$', text, re.M)
    assert ids == [item[0] for item in visibility.CONCERNS]
    assert questions == [item[1] for item in visibility.CONCERNS]
    assert profiles == [item[2] for item in visibility.CONCERNS]
    assert len(blocks) == len(set(ids)) == 10
    assert all("required = true" in block for block in blocks)
    for forbidden in ("command =", "severity =", "cadence =", "page =",
                      "notify =", "remediation ="):
        assert forbidden not in text


def test_empty_checkout_is_explicitly_absent_or_unknown_not_green(tmp_path):
    document = visibility.build_status(tmp_path / "absent.sqlite",
                                       tmp_path / "absent-report", now=NOW)
    concerns = _indexed(document)
    assert len(concerns) == len(visibility.CONCERNS)
    assert all(item["required"] for item in concerns.values())
    assert concerns["weatherwatch.persistence.access"]["state"] == "ABSENT"
    assert concerns["weatherwatch.observation.coverage"]["state"] == "ABSENT"
    assert concerns["weatherwatch.acquisition.connection"]["state"] == "UNKNOWN"
    assert concerns["weatherwatch.aggregate.production"]["state"] == "ABSENT"
    assert concerns["weatherwatch.report.candidate"]["state"] == "ABSENT"
    assert concerns["weatherwatch.publication.gate"]["state"] == "ABSENT"
    assert document["authority"] == {
        "declaration_is_observation": False,
        "observation_is_admitted_evidence": False,
        "observation_is_current_qualification": False,
        "candidate_is_publication_authority": False,
    }
    privacy = document["extensions"]["weatherwatch"]["privacy"]
    assert privacy["weather_database_identity_free"] is None
    assert privacy["raw_events_retained"] is None


def test_normal_active_observation_is_supported_by_current_local_facts(conn, tmp_path):
    _active(conn, {"metrics": {"post.create": 12}, "coverage_state": "ok"},
            cursor=(SYNTH_BASE + 50) * 1_000_000)
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))
    for cid in ("weatherwatch.observation.coverage",
                "weatherwatch.acquisition.connection",
                "weatherwatch.acquisition.cursor",
                "weatherwatch.acquisition.delay",
                "weatherwatch.observation.loss",
                "weatherwatch.persistence.access",
                "weatherwatch.persistence.continuity",
                "weatherwatch.aggregate.production"):
        assert concerns[cid]["state"] == "PRESENT", (cid, concerns[cid])
    assert concerns["weatherwatch.acquisition.cursor"]["facts"]["advance_state"] == "ADVANCING"


def test_observed_empty_is_not_observer_blind_or_cursor_failure(conn, tmp_path):
    _active(conn, {"metrics": {}, "events_seen": 0, "coverage_state": "ok"})
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))
    assert concerns["weatherwatch.observation.coverage"]["state"] == "PRESENT"
    cursor = concerns["weatherwatch.acquisition.cursor"]
    assert cursor["state"] == "PRESENT"
    assert cursor["facts"]["advance_state"] == "NO_ACTIVITY_OBSERVED"
    aggregate = concerns["weatherwatch.aggregate.production"]
    assert aggregate["state"] == "PRESENT"
    assert aggregate["facts"]["activity_state"] == "EMPTY"
    # No event means there is no new lag sample. That narrow question remains
    # unknown while coverage and the explicit empty product remain present.
    assert concerns["weatherwatch.acquisition.delay"]["state"] == "UNKNOWN"


def test_gaps_parser_failures_and_excessive_lag_are_degraded(conn, tmp_path):
    _active(conn, {
        "metrics": {"post.create": 2}, "events_seen": 2,
        "coverage_state": "degraded", "gate_reasons": "gap_observed,lag_high",
        "gap_us": 3_000_000, "parse_errors": 2, "rejected": 1,
        "late": 1, "lag_ewma_s": 130.0,
    }, cursor=(SYNTH_BASE + 40) * 1_000_000)
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))
    assert concerns["weatherwatch.observation.coverage"]["state"] == "DEGRADED"
    assert concerns["weatherwatch.observation.loss"]["state"] == "DEGRADED"
    assert concerns["weatherwatch.acquisition.delay"]["state"] == "DEGRADED"
    counters = concerns["weatherwatch.observation.loss"]["facts"]["counters"]
    assert counters["gap_us"] == 3_000_000
    assert counters["parse_errors"] == 2


def test_cursor_that_does_not_advance_past_restart_basis_is_degraded(conn, tmp_path):
    start = (SYNTH_BASE + 20) * 1_000_000
    build_run(conn, "r1", [{"metrics": {"post.create": 2}}], ended_at=None,
              resume_cursor=start)
    db.set_meta(conn, db.cursor_key(SYNTH_ENDPOINT), str(start))
    _runtime(conn)
    cursor = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))[
            "weatherwatch.acquisition.cursor"]
    assert cursor["state"] == "DEGRADED"
    assert cursor["facts"]["advance_state"] == "NOT_ADVANCING"


def test_stale_window_and_stale_connected_marker_are_not_current(conn, tmp_path):
    _active(conn, {"metrics": {"post.create": 1}, "coverage_state": "ok"},
            cursor=(SYNTH_BASE + 30) * 1_000_000)
    later = NOW + dt.timedelta(hours=1)
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=later))
    for cid in ("weatherwatch.observation.coverage",
                "weatherwatch.acquisition.connection",
                "weatherwatch.acquisition.cursor",
                "weatherwatch.acquisition.delay",
                "weatherwatch.observation.loss",
                "weatherwatch.aggregate.production"):
        assert concerns[cid]["state"] == "STALE", (cid, concerns[cid])


def test_disconnected_and_reconnecting_are_explicitly_degraded(conn, tmp_path):
    _active(conn, {"metrics": {"post.create": 1}, "coverage_state": "ok"},
            cursor=(SYNTH_BASE + 30) * 1_000_000)
    for state in ("disconnected", "reconnecting"):
        _runtime(conn, state)
        concern = _indexed(visibility.build_status(
            _path(conn), tmp_path / state, now=NOW))[
                "weatherwatch.acquisition.connection"]
        assert concern["state"] == "DEGRADED"
        assert concern["facts"]["runtime_state"] == state


def test_restart_does_not_relabel_historical_state_as_present(conn, tmp_path):
    build_run(conn, "r1", [{"metrics": {"post.create": 5}}],
              started_at="2026-01-01T00:00:00+00:00")
    db.set_meta(conn, db.cursor_key(SYNTH_ENDPOINT),
                str((SYNTH_BASE + 50) * 1_000_000))
    build_run(conn, "r2", [], ended_at=None,
              started_at="2026-01-01T01:00:00+00:00",
              resume_cursor=(SYNTH_BASE + 50) * 1_000_000)
    _runtime(conn, "starting")
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))
    assert concerns["weatherwatch.observation.coverage"]["state"] == "UNKNOWN"
    assert concerns["weatherwatch.acquisition.connection"]["state"] == "UNKNOWN"
    assert concerns["weatherwatch.acquisition.cursor"]["state"] == "UNKNOWN"
    assert concerns["weatherwatch.aggregate.production"]["state"] == "ABSENT"


def test_ended_run_cursor_and_lag_are_historical_not_present(conn, tmp_path):
    build_run(conn, "r1", [{"metrics": {"post.create": 5}}])
    db.set_meta(conn, db.cursor_key(SYNTH_ENDPOINT),
                str((SYNTH_BASE + 50) * 1_000_000))
    _runtime(conn, "stopped")
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))
    assert concerns["weatherwatch.observation.coverage"]["state"] == "ABSENT"
    assert concerns["weatherwatch.acquisition.connection"]["state"] == "ABSENT"
    assert concerns["weatherwatch.acquisition.cursor"]["state"] == "ABSENT"
    assert concerns["weatherwatch.acquisition.delay"]["state"] == "ABSENT"


def test_runtime_provenance_mismatch_cannot_claim_connection(conn, tmp_path):
    _active(conn, {"metrics": {"post.create": 1}, "coverage_state": "ok"},
            cursor=(SYNTH_BASE + 30) * 1_000_000)
    runtime = json.loads(db.get_meta(conn, visibility.RUNTIME_META_KEY))
    runtime["run_id"] = "substituted-run"
    db.set_meta(conn, visibility.RUNTIME_META_KEY, json.dumps(runtime))
    concerns = _indexed(visibility.build_status(
        _path(conn), tmp_path / "report", now=NOW))
    assert concerns["weatherwatch.acquisition.connection"]["state"] == "UNKNOWN"
    continuity = concerns["weatherwatch.persistence.continuity"]
    assert continuity["state"] == "DEGRADED"
    assert "runtime_binding_mismatch" in continuity["facts"]["failures"]


def test_corrupt_persistence_is_degraded_and_missing_instrumentation_is_not_green(tmp_path):
    path = tmp_path / "broken.sqlite"
    path.write_bytes(b"not sqlite")
    concerns = _indexed(visibility.build_status(
        path, tmp_path / "report", now=NOW))
    assert concerns["weatherwatch.persistence.access"]["state"] == "DEGRADED"
    assert concerns["weatherwatch.persistence.continuity"]["state"] == "UNKNOWN"
    assert concerns["weatherwatch.observation.coverage"]["state"] == "UNKNOWN"


def test_stale_candidate_is_reevaluated_now_and_does_not_refresh_itself(tmp_path):
    report_dir = tmp_path / "report"
    old = NOW - dt.timedelta(hours=2)
    _candidate(report_dir, generated=old, newest=old,
               recorded_state="current")
    concerns = _indexed(visibility.build_status(
        tmp_path / "absent.sqlite", report_dir, now=NOW))
    report = concerns["weatherwatch.report.candidate"]
    assert report["state"] == "STALE"
    assert report["facts"]["recorded_freshness_state"] == "current"
    assert report["facts"]["currentness_reevaluated_at"] == NOW.isoformat().replace("+00:00", "Z")
    # The structural/privacy gate may pass an old candidate. Its independent
    # report-currentness concern still prevents generic tooling from calling
    # the required surface green.
    assert concerns["weatherwatch.publication.gate"]["state"] == "PRESENT"
    assert concerns["weatherwatch.publication.gate"]["facts"]["publication_authority"] is False


def test_publish_gate_refusal_is_not_infrastructure_failure_or_authority(tmp_path):
    report_dir = tmp_path / "report"
    _candidate(report_dir, generated=NOW, newest=NOW)
    (report_dir / "index.html").write_text("did:plc:synthetic-secret")
    gate = evaluate_candidate(report_dir)
    assert gate["disposition"] == "REFUSED"
    assert gate["publication_authority"] is False
    assert gate["published"] is False
    assert gate["refusals"][0]["kind"] == "identity_shaped_value"
    assert "synthetic-secret" not in json.dumps(gate)
    concerns = _indexed(visibility.build_status(
        tmp_path / "absent.sqlite", report_dir, now=NOW))
    assert concerns["weatherwatch.publication.gate"]["state"] == "REFUSED"


def test_incomplete_candidate_is_degraded_even_with_fresh_summary(tmp_path):
    report_dir = tmp_path / "report"
    _candidate(report_dir, generated=NOW, newest=NOW)
    (report_dir / "index.html").unlink()
    concerns = _indexed(visibility.build_status(
        tmp_path / "absent.sqlite", report_dir, now=NOW))
    assert concerns["weatherwatch.report.candidate"]["state"] == "DEGRADED"
    assert concerns["weatherwatch.publication.gate"]["state"] == "REFUSED"


def test_human_status_covers_the_required_operator_sections(tmp_path):
    rendered = visibility.render_human(visibility.build_status(
        tmp_path / "absent.sqlite", tmp_path / "report", now=NOW))
    for heading in ("acquisition / coverage", "cursor / freshness",
                    "loss / gaps", "aggregate", "persistence",
                    "report / publish"):
        assert heading in rendered
    assert "Candidate output is not publication authority" in rendered
