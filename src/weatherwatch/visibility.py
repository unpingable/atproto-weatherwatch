"""Versioned, identity-free local visibility for generic observers.

The concern declaration is inventory.  This module is the observation
producer: it reads Weatherwatch's existing durable facts and the rendered
candidate, evaluates only locally knowable state, and refuses to turn old or
missing facts into present health.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

from . import COLLECTOR_VERSION, db, timeutil
from .accumulator import LATE_EVENT_GRACE_S
from .health import LAG_HIGH_THRESHOLD_S
from .publication import evaluate_candidate
from .report import PUBLICATION_INTERVAL_S


SCHEMA = "project.ops.status/v1"
MANIFEST_SCHEMA = "project.concerns/v1"
MANIFEST_PATH = ".ops/concerns.toml"
RUNTIME_META_KEY = "collector_runtime:v1"

PRESENT = "PRESENT"
DEGRADED = "DEGRADED"
UNKNOWN = "UNKNOWN"
STALE = "STALE"
ABSENT = "ABSENT"
REFUSED = "REFUSED"

CONCERNS = (
    ("weatherwatch.observation.coverage",
     "weatherwatch.question.observation-coverage-known",
     "weatherwatch.profile.observation-coverage.v1"),
    ("weatherwatch.acquisition.connection",
     "weatherwatch.question.acquisition-connected",
     "weatherwatch.profile.acquisition-connection.v1"),
    ("weatherwatch.acquisition.cursor",
     "weatherwatch.question.ingest-cursor-current",
     "weatherwatch.profile.ingest-cursor-current.v1"),
    ("weatherwatch.acquisition.delay",
     "weatherwatch.question.observation-delay-bounded",
     "weatherwatch.profile.observation-delay.v1"),
    ("weatherwatch.observation.loss",
     "weatherwatch.question.known-loss-absent",
     "weatherwatch.profile.known-loss.v1"),
    ("weatherwatch.persistence.access",
     "weatherwatch.question.durable-state-accessible",
     "weatherwatch.profile.durable-state-access.v1"),
    ("weatherwatch.persistence.continuity",
     "weatherwatch.question.cursor-meta-coherent",
     "weatherwatch.profile.cursor-meta-coherence.v1"),
    ("weatherwatch.aggregate.production",
     "weatherwatch.question.aggregate-window-current",
     "weatherwatch.profile.aggregate-production.v1"),
    ("weatherwatch.report.candidate",
     "weatherwatch.question.candidate-report-current",
     "weatherwatch.profile.candidate-report.v1"),
    ("weatherwatch.publication.gate",
     "weatherwatch.question.publish-gate-passes",
     "weatherwatch.profile.publish-gate.v1"),
)
_IDENTITY = {item[0]: item[1:] for item in CONCERNS}


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _age(now: dt.datetime, value: dt.datetime | None) -> float | None:
    return max(0.0, (now - value).total_seconds()) if value else None


def _concern(concern_id: str, state: str, summary: str,
             facts: dict[str, Any], observed_at: str | None = None) -> dict:
    question_id, profile_id = _IDENTITY[concern_id]
    return {
        "concern_id": concern_id,
        "question_id": question_id,
        "profile_id": profile_id,
        "required": True,
        "state": state,
        "summary": summary,
        "observed_at": observed_at,
        "facts": facts,
    }


def _open_database(path: Path) -> sqlite3.Connection:
    # mode=rw is deliberate: status must not create absent state and call it
    # healthy. BEGIN IMMEDIATE below tests whether a write transaction can be
    # acquired, then rolls it back without changing application data.
    uri = f"file:{path.resolve().as_posix()}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def _database_snapshot(path: Path) -> tuple[dict, sqlite3.Connection | None]:
    snap: dict[str, Any] = {
        "path": str(path), "exists": path.exists(), "readable": False,
        "write_transaction_available": False, "quick_check": None,
        "error": None,
    }
    if not path.exists():
        return snap, None
    conn = None
    try:
        conn = _open_database(path)
        snap["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        snap["readable"] = snap["quick_check"] == "ok"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        snap["write_transaction_available"] = True
        return snap, conn
    except sqlite3.Error as exc:
        if conn is not None and conn.in_transaction:
            conn.execute("ROLLBACK")
        if conn is not None:
            conn.close()
        snap["error"] = type(exc).__name__
        return snap, None


def _db_facts(conn: sqlite3.Connection) -> dict:
    tables = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    required_tables = {"meta", "observation_run", "bucket", "window_health"}
    missing_tables = sorted(required_tables - set(tables))
    schema_version = None
    runtime = None
    cursor_errors: list[str] = []
    cursors: dict[str, int] = {}
    if "meta" in tables:
        rows = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        for row in rows:
            key, value = row["key"], row["value"]
            if key == "schema_version":
                schema_version = value
            elif key == RUNTIME_META_KEY:
                try:
                    runtime = json.loads(value)
                except (TypeError, ValueError):
                    runtime = {"invalid": True}
            elif key.startswith("cursor:"):
                try:
                    cursors[key.removeprefix("cursor:")] = int(value)
                except (TypeError, ValueError):
                    cursor_errors.append(key)

    latest_run = None
    latest_health = None
    if not missing_tables:
        latest_run_row = conn.execute(
            "SELECT * FROM observation_run "
            "ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        if latest_run_row:
            latest_run = dict(latest_run_row)
            health_row = conn.execute(
                "SELECT * FROM window_health WHERE run_id=? "
                "ORDER BY bucket_start DESC LIMIT 1",
                (latest_run_row["run_id"],)).fetchone()
            latest_health = dict(health_row) if health_row else None

    columns = {}
    for table in sorted(required_tables & set(tables)):
        columns[table] = [r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})")]
    identity_names = {"did", "handle", "rkey", "cid", "uri", "text",
                      "actor", "subject"}
    identity_columns = sorted(
        f"{table}.{column}" for table, names in columns.items()
        for column in names if column.lower() in identity_names)
    raw_event_tables = sorted(set(tables) & {"event", "events", "raw_event",
                                             "raw_events"})
    return {
        "tables": tables,
        "missing_tables": missing_tables,
        "schema_version": schema_version,
        "runtime": runtime,
        "cursors": cursors,
        "cursor_errors": cursor_errors,
        "latest_run": latest_run,
        "latest_health": latest_health,
        "identity_columns": identity_columns,
        "raw_event_tables": raw_event_tables,
    }


def _window_context(snapshot: dict, now: dt.datetime) -> dict:
    run = snapshot.get("latest_run")
    health = snapshot.get("latest_health")
    if not run:
        return {"run": None, "health": None, "current": False,
                "age_seconds": None, "threshold_seconds": None,
                "observed_at": None}
    width = int(run["bucket_width"])
    threshold = 2 * width + LATE_EVENT_GRACE_S
    if not health:
        return {"run": run, "health": None, "current": False,
                "age_seconds": None, "threshold_seconds": threshold,
                "observed_at": None}
    end = dt.datetime.fromtimestamp(
        health["bucket_start"] + health["bucket_width"], dt.timezone.utc)
    age = _age(now, end)
    return {"run": run, "health": health,
            "current": bool(age is not None and age <= threshold),
            "age_seconds": age, "threshold_seconds": threshold,
            "observed_at": _iso(end)}


def _database_concerns(access: dict, snapshot: dict | None,
                       now: dt.datetime) -> list[dict]:
    if not access["exists"]:
        persistence = _concern(
            "weatherwatch.persistence.access", ABSENT,
            "durable Weatherwatch database has not been created", access)
    elif not access["readable"] or not access["write_transaction_available"]:
        persistence = _concern(
            "weatherwatch.persistence.access", DEGRADED,
            "durable state is not both readable and write-transaction capable",
            access)
    else:
        persistence = _concern(
            "weatherwatch.persistence.access", PRESENT,
            "durable state is readable and a write transaction can be acquired",
            access)

    if snapshot is None:
        unavailable = [
            persistence,
            _concern("weatherwatch.persistence.continuity", UNKNOWN,
                     "cursor/meta coherence cannot be evaluated", access),
        ]
        for cid, summary in (
            ("weatherwatch.observation.coverage", "observation coverage is unavailable"),
            ("weatherwatch.acquisition.connection", "connection state is unavailable"),
            ("weatherwatch.acquisition.cursor", "cursor state is unavailable"),
            ("weatherwatch.acquisition.delay", "observation delay is unavailable"),
            ("weatherwatch.observation.loss", "known-loss counters are unavailable"),
            ("weatherwatch.aggregate.production", "aggregate output is unavailable"),
        ):
            state = ABSENT if not access["exists"] and cid in {
                "weatherwatch.observation.coverage",
                "weatherwatch.acquisition.cursor",
                "weatherwatch.aggregate.production",
            } else UNKNOWN
            unavailable.append(_concern(cid, state, summary, access))
        return unavailable

    coherence_failures = []
    if snapshot["missing_tables"]:
        coherence_failures.append("required_tables_missing")
    if snapshot["schema_version"] != db.SCHEMA_VERSION:
        coherence_failures.append("schema_version_mismatch")
    if snapshot["cursor_errors"]:
        coherence_failures.append("cursor_not_integer")
    if snapshot["runtime"] and snapshot["runtime"].get("invalid"):
        coherence_failures.append("runtime_meta_invalid")
    if snapshot["runtime"] and not snapshot["runtime"].get("invalid"):
        if snapshot["runtime"].get("schema") != "weatherwatch.collector_runtime.v1":
            coherence_failures.append("runtime_schema_mismatch")
        latest = snapshot["latest_run"]
        if latest and (snapshot["runtime"].get("run_id") != latest["run_id"] or
                       snapshot["runtime"].get("endpoint") !=
                       latest["source_endpoint"]):
            coherence_failures.append("runtime_binding_mismatch")
    if snapshot["identity_columns"]:
        coherence_failures.append("identity_columns_present")
    if snapshot["raw_event_tables"]:
        coherence_failures.append("raw_event_tables_present")
    continuity_facts = {
        "schema_version": snapshot["schema_version"],
        "expected_schema_version": db.SCHEMA_VERSION,
        "missing_tables": snapshot["missing_tables"],
        "cursor_keys": sorted(snapshot["cursors"]),
        "cursor_errors": snapshot["cursor_errors"],
        "identity_columns_present": snapshot["identity_columns"],
        "raw_event_tables_present": snapshot["raw_event_tables"],
        "cursor_is_endpoint_scoped": True,
        "cursor_and_windows_commit_atomically": True,
    }
    continuity = _concern(
        "weatherwatch.persistence.continuity",
        DEGRADED if coherence_failures else PRESENT,
        ("cursor/meta persistence has coherence failures" if coherence_failures
         else "cursor/meta persistence is structurally coherent"),
        {**continuity_facts, "failures": coherence_failures})

    ctx = _window_context(snapshot, now)
    run, health = ctx["run"], ctx["health"]
    runtime = snapshot.get("runtime")
    common = {
        "run_id": run["run_id"] if run else None,
        "run_state": ("OPEN" if run and run["ended_at"] is None else
                      "ENDED" if run else "ABSENT"),
        "endpoint": run["source_endpoint"] if run else None,
        "latest_window_end": ctx["observed_at"],
        "latest_window_age_seconds": ctx["age_seconds"],
        "currentness_profile_seconds": ctx["threshold_seconds"],
    }

    if not run:
        coverage = _concern("weatherwatch.observation.coverage", ABSENT,
                            "no observation run has been recorded", common)
    elif run["ended_at"] is not None:
        coverage = _concern(
            "weatherwatch.observation.coverage", ABSENT,
            "the latest observation run has ended; historical coverage is not present coverage",
            {**common, "historical_coverage_state":
             health["coverage_state"] if health else None}, ctx["observed_at"])
    elif not health:
        coverage = _concern(
            "weatherwatch.observation.coverage", UNKNOWN,
            "the open run has not produced enough persisted observation evidence",
            common)
    elif not ctx["current"]:
        coverage = _concern(
            "weatherwatch.observation.coverage", STALE,
            "the latest persisted coverage observation is stale", common,
            ctx["observed_at"])
    elif health["coverage_state"] == "degraded":
        coverage = _concern(
            "weatherwatch.observation.coverage", DEGRADED,
            "the current coverage state is degraded",
            {**common, "coverage_state": health["coverage_state"],
             "gate_reasons": _reasons(health)}, ctx["observed_at"])
    elif health["coverage_state"] == "ok":
        coverage = _concern(
            "weatherwatch.observation.coverage", PRESENT,
            "current observed coverage is supported by an instrumented window",
            {**common, "coverage_state": "ok"}, ctx["observed_at"])
    else:
        coverage = _concern(
            "weatherwatch.observation.coverage", UNKNOWN,
            "coverage is still warming up or is not understood",
            {**common, "coverage_state": health["coverage_state"]},
            ctx["observed_at"])

    connection = _connection_concern(runtime, common, now)
    cursor = _cursor_concern(snapshot, ctx, common)
    delay = _delay_concern(ctx, common)
    loss = _loss_concern(ctx, common)
    aggregate = _aggregate_concern(ctx, common)
    return [persistence, continuity, coverage, connection, cursor, delay,
            loss, aggregate]


def _reasons(health: dict) -> list[str]:
    return sorted(r for r in (health.get("gate_reasons") or "").split(",") if r)


def _connection_concern(runtime: dict | None, common: dict,
                        now: dt.datetime) -> dict:
    cid = "weatherwatch.acquisition.connection"
    if not runtime or runtime.get("invalid"):
        return _concern(cid, UNKNOWN,
                        "collector connection instrumentation is absent or invalid",
                        {**common, "runtime": runtime})
    updated = _parse_time(runtime.get("updated_at"))
    age = _age(now, updated)
    threshold = common["currentness_profile_seconds"] or 125.0
    facts = {**common, "runtime_state": runtime.get("state"),
             "runtime_run_id": runtime.get("run_id"),
             "runtime_endpoint": runtime.get("endpoint"),
             "runtime_updated_at": runtime.get("updated_at"),
             "runtime_age_seconds": age,
             "currentness_profile_seconds": threshold,
             "last_error_kind": runtime.get("last_error_kind")}
    if (runtime.get("run_id") != common["run_id"] or
            runtime.get("endpoint") != common["endpoint"]):
        return _concern(
            cid, UNKNOWN,
            "collector runtime testimony does not bind the latest run and endpoint",
            facts, runtime.get("updated_at"))
    if age is None:
        return _concern(cid, UNKNOWN, "runtime state has no usable clock", facts)
    if age > threshold:
        return _concern(cid, STALE,
                        "collector runtime state is stale and cannot establish a present connection",
                        facts, runtime.get("updated_at"))
    state = runtime.get("state")
    if state == "connected":
        return _concern(cid, PRESENT, "collector reports a current WSS connection",
                        facts, runtime.get("updated_at"))
    if state in {"disconnected", "reconnecting"}:
        return _concern(cid, DEGRADED, f"collector is {state}", facts,
                        runtime.get("updated_at"))
    if state == "stopped":
        return _concern(cid, ABSENT, "collector is explicitly stopped", facts,
                        runtime.get("updated_at"))
    return _concern(cid, UNKNOWN, f"collector is {state or 'in an unknown state'}",
                    facts, runtime.get("updated_at"))


def _cursor_concern(snapshot: dict, ctx: dict, common: dict) -> dict:
    cid = "weatherwatch.acquisition.cursor"
    run, health = ctx["run"], ctx["health"]
    if not run:
        return _concern(cid, ABSENT, "no ingest cursor is expected before observation", common)
    cursor = snapshot["cursors"].get(run["source_endpoint"])
    facts = {**common, "cursor_time_us": cursor,
             "resume_cursor_at_start": run["resume_cursor_at_start"],
             "events_seen": health["events_seen"] if health else None,
             "advance_state": "UNKNOWN"}
    if not health:
        return _concern(cid, UNKNOWN,
                        "no current window proves that the historical cursor is advancing",
                        facts)
    if run["ended_at"] is not None:
        facts["advance_state"] = "STOPPED"
        return _concern(
            cid, ABSENT,
            "the latest run has ended; its persisted cursor is historical continuity state",
            facts, ctx["observed_at"])
    if not ctx["current"]:
        return _concern(cid, STALE, "cursor support comes from a stale window",
                        facts, ctx["observed_at"])
    if health["events_seen"] == 0 and health["observed_duration_us"] > 0:
        facts["advance_state"] = "NO_ACTIVITY_OBSERVED"
        return _concern(
            cid, PRESENT,
            "cursor did not need to advance during an explicitly observed empty window",
            facts, ctx["observed_at"])
    if cursor is None:
        facts["advance_state"] = "MISSING"
        return _concern(cid, DEGRADED,
                        "events were observed but no endpoint-scoped cursor is persisted",
                        facts, ctx["observed_at"])
    if cursor > health["observed_to_us"]:
        facts["advance_state"] = "INCOHERENT_AHEAD"
        return _concern(cid, DEGRADED,
                        "cursor points beyond the latest durable observation window",
                        facts, ctx["observed_at"])
    start = run["resume_cursor_at_start"]
    if start is not None and cursor <= start:
        facts["advance_state"] = "NOT_ADVANCING"
        return _concern(cid, DEGRADED,
                        "events were observed but the cursor has not advanced beyond run start",
                        facts, ctx["observed_at"])
    facts["advance_state"] = "ADVANCING"
    return _concern(cid, PRESENT,
                    "endpoint-scoped cursor is current and backed by durable aggregate state",
                    facts, ctx["observed_at"])


def _delay_concern(ctx: dict, common: dict) -> dict:
    cid = "weatherwatch.acquisition.delay"
    health = ctx["health"]
    if not health:
        return _concern(cid, UNKNOWN, "no persisted lag measurement is available", common)
    facts = {**common, "lag_ewma_seconds": health["lag_ewma_s"],
             "lag_max_seconds": health["lag_max_s"],
             "excessive_lag_seconds": LAG_HIGH_THRESHOLD_S,
             "events_seen": health["events_seen"]}
    if ctx["run"]["ended_at"] is not None:
        return _concern(cid, ABSENT,
                        "the latest run has ended; its lag sample is historical",
                        facts, ctx["observed_at"])
    if not ctx["current"]:
        return _concern(cid, STALE, "the latest lag measurement is stale", facts,
                        ctx["observed_at"])
    if health["events_seen"] == 0:
        return _concern(cid, UNKNOWN,
                        "an observed empty window supplies no new event-lag sample",
                        facts, ctx["observed_at"])
    reasons = _reasons(health)
    if "lag_high" in reasons or (health["lag_ewma_s"] is not None and
                                  health["lag_ewma_s"] > LAG_HIGH_THRESHOLD_S):
        return _concern(cid, DEGRADED, "observation delay is excessive", facts,
                        ctx["observed_at"])
    if health["lag_ewma_s"] is None:
        return _concern(cid, UNKNOWN, "lag was not measured", facts,
                        ctx["observed_at"])
    return _concern(cid, PRESENT, "observation delay is within the existing health profile",
                    facts, ctx["observed_at"])


def _loss_concern(ctx: dict, common: dict) -> dict:
    cid = "weatherwatch.observation.loss"
    health = ctx["health"]
    if not health:
        return _concern(cid, UNKNOWN, "known-loss counters have not been observed", common)
    counters = {name: int(health[name]) for name in (
        "parse_errors", "rejected_no_time_us", "late_events", "unclassified",
        "gap_us", "reconnects")}
    facts = {**common, "counters": counters, "resume_seam": bool(health["resume_seam"]),
             "loss_buckets_instrumented": True}
    if not ctx["current"]:
        return _concern(cid, STALE, "known-loss counters are stale", facts,
                        ctx["observed_at"])
    loss = sum(counters[name] for name in (
        "parse_errors", "rejected_no_time_us", "late_events", "unclassified",
        "gap_us"))
    if loss:
        return _concern(cid, DEGRADED, "the latest window records known loss or ambiguity",
                        facts, ctx["observed_at"])
    return _concern(cid, PRESENT, "all instrumented known-loss counters are zero",
                    facts, ctx["observed_at"])


def _aggregate_concern(ctx: dict, common: dict) -> dict:
    cid = "weatherwatch.aggregate.production"
    health = ctx["health"]
    if not health:
        return _concern(cid, ABSENT,
                        "the latest run has not produced an aggregate window", common)
    activity = "EMPTY" if health["events_seen"] == 0 else "ACTIVE"
    facts = {**common, "activity_state": activity,
             "events_seen": health["events_seen"],
             "observed_duration_us": health["observed_duration_us"],
             "partial": bool(health["partial"]),
             "window_health_row_present": True,
             "empty_window_is_product": True}
    if not ctx["current"]:
        return _concern(cid, STALE, "the latest aggregate window is stale", facts,
                        ctx["observed_at"])
    return _concern(cid, PRESENT,
                    f"a current aggregate window exists and records {activity.lower()} activity",
                    facts, ctx["observed_at"])


def _report_concerns(report_dir: Path, now: dt.datetime) -> list[dict]:
    gate = evaluate_candidate(report_dir)
    candidate_facts: dict[str, Any] = {
        "report_dir": str(report_dir),
        "candidate_exists": report_dir.is_dir(),
        "candidate_is_authority": False,
        "publication_observed": False,
    }
    summary_path = report_dir / "summary.json"
    if not summary_path.is_file():
        report_state = ABSENT
        report_summary = "no complete candidate report exists"
        observed_at = None
    else:
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            generated = _parse_time(summary.get("generated_at"))
            freshness = summary.get("freshness") or {}
            newest = _parse_time(freshness.get("newest_observation_end"))
            source_limit = freshness.get("current_threshold_seconds")
            source_age = _age(now, newest)
            artifact_age = _age(now, generated)
            artifact_limit = 2 * PUBLICATION_INTERVAL_S
            candidate_facts.update({
                "generated_at": summary.get("generated_at"),
                "artifact_age_seconds": artifact_age,
                "artifact_currentness_profile_seconds": artifact_limit,
                "recorded_freshness_state": freshness.get("state"),
                "newest_observation_end": freshness.get("newest_observation_end"),
                "source_age_seconds": source_age,
                "source_currentness_profile_seconds": source_limit,
                "currentness_reevaluated_at": _iso(now),
            })
            if generated is None or not isinstance(source_limit, (int, float)):
                report_state, report_summary = UNKNOWN, "candidate currentness facts are incomplete"
            elif artifact_age is not None and artifact_age > artifact_limit:
                report_state, report_summary = STALE, "candidate report artifact is stale"
            elif newest is None:
                report_state, report_summary = UNKNOWN, "candidate contains no observed aggregate window"
            elif source_age is not None and source_age > source_limit:
                report_state, report_summary = STALE, "candidate report is backed by stale aggregate state"
            elif freshness.get("state") == "partial":
                report_state, report_summary = DEGRADED, "candidate's newest observation is partial"
            elif freshness.get("state") == "unavailable":
                report_state, report_summary = UNKNOWN, "candidate reports observation unavailable"
            else:
                report_state, report_summary = PRESENT, "a current candidate report exists"
            observed_at = summary.get("generated_at")
        except (OSError, ValueError, TypeError) as exc:
            candidate_facts["error"] = type(exc).__name__
            report_state, report_summary, observed_at = DEGRADED, "candidate report metadata is unreadable", None
    if any(item.get("kind") == "candidate_incomplete"
           for item in gate["refusals"]):
        report_state = DEGRADED
        report_summary = "candidate report is structurally incomplete"
    report_concern = _concern("weatherwatch.report.candidate", report_state,
                              report_summary, candidate_facts, observed_at)

    disposition = gate["disposition"]
    gate_state = {"PASSED": PRESENT, "REFUSED": REFUSED,
                  "ERROR": DEGRADED, "NOT_EVALUATED": ABSENT}[disposition]
    gate_summary = {
        "PASSED": "local candidate passed the publication gate; this grants no authority",
        "REFUSED": "local candidate was refused by the publication gate",
        "ERROR": "publication gate could not evaluate the candidate",
        "NOT_EVALUATED": "publication gate has no candidate to evaluate",
    }[disposition]
    return [report_concern, _concern(
        "weatherwatch.publication.gate", gate_state, gate_summary, gate)]


def build_status(db_path: str | Path = db.DEFAULT_DB_PATH,
                 report_dir: str | Path = "build/report",
                 now: dt.datetime | None = None) -> dict:
    """Build one deterministic visibility document from current local facts."""
    now = (now or timeutil.now_utc()).astimezone(dt.timezone.utc)
    db_path, report_dir = Path(db_path), Path(report_dir)
    access, conn = _database_snapshot(db_path)
    snapshot = None
    if conn is not None:
        try:
            snapshot = _db_facts(conn)
        except sqlite3.Error as exc:
            access["error"] = type(exc).__name__
            access["readable"] = False
        finally:
            conn.close()
    concerns = _database_concerns(access, snapshot, now)
    concerns.extend(_report_concerns(report_dir, now))
    by_id = {item["concern_id"]: item for item in concerns}
    ordered = [by_id[cid] for cid, _, _ in CONCERNS]
    manifest_path = Path(__file__).resolve().parents[2] / MANIFEST_PATH
    with manifest_path.open("rb") as handle:
        declarations = tomllib.load(handle)["concerns"]
    declared = {item["id"]: item for item in declarations}
    generic_concerns = []
    for item in ordered:
        declaration = declared[item["concern_id"]]
        generic_concerns.append({
            "id": item["concern_id"],
            "question": item["question_id"],
            "profile": item["profile_id"],
            "required": item["required"],
            "description": declaration["description"],
            "observation": {
                "observation_present": True,
                "local_state": item["state"],
                "domain_state": None,
                "observed_at": item["observed_at"],
                "valid_for_seconds": None,
                "reason": item["summary"],
                "facts": item["facts"],
            },
        })
    return {
        "schema": SCHEMA,
        "generated_at": _iso(now),
        "project": "weatherwatch",
        "manifest": {"schema": MANIFEST_SCHEMA, "path": MANIFEST_PATH},
        "producer": {"id": "weatherwatch.status", "version": COLLECTOR_VERSION},
        "authority": {
            "declaration_is_observation": False,
            "observation_is_admitted_evidence": False,
            "observation_is_current_qualification": False,
            "candidate_is_publication_authority": False,
        },
        "extensions": {
            "weatherwatch": {
                "privacy": {
                    "weather_database_identity_free": (
                        not bool(snapshot["identity_columns"])
                        if snapshot is not None else None),
                    "raw_events_retained": (
                        bool(snapshot["raw_event_tables"])
                        if snapshot is not None else None),
                    "status_contains_subject_observations": False,
                },
            },
        },
        "concerns": generic_concerns,
    }


def render_human(document: dict) -> str:
    """Concise operator view over the same document returned as JSON."""
    groups = (
        ("acquisition / coverage", ("weatherwatch.observation.coverage",
                                    "weatherwatch.acquisition.connection")),
        ("cursor / freshness", ("weatherwatch.acquisition.cursor",
                                "weatherwatch.acquisition.delay")),
        ("loss / gaps", ("weatherwatch.observation.loss",)),
        ("aggregate", ("weatherwatch.aggregate.production",)),
        ("persistence", ("weatherwatch.persistence.access",
                         "weatherwatch.persistence.continuity")),
        ("report / publish", ("weatherwatch.report.candidate",
                              "weatherwatch.publication.gate")),
    )
    indexed = {c["id"]: c for c in document["concerns"]}
    lines = [f"Weatherwatch visibility  {document['generated_at']}"]
    for title, ids in groups:
        lines.append(f"\n{title}")
        for cid in ids:
            item = indexed[cid]["observation"]
            name = cid.removeprefix("weatherwatch.")
            lines.append(
                f"  {item['local_state']:<9} {name:<30} {item['reason']}")
    lines.extend((
        "",
        "Candidate output is not publication authority. Declaration is not observation;",
        "observation is not NQ admission; historical status is not current qualification.",
    ))
    return "\n".join(lines)
