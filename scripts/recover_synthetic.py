#!/usr/bin/env python3
"""Rehearse Weatherwatch application recovery using the fixed synthetic fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from weatherwatch import COLLECTOR_VERSION, db, report
from weatherwatch.accumulator import Accumulator
from weatherwatch.classify import classify

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/jetstream_synthetic.jsonl"
FIXTURE_SHA256 = "ee9b72b4e0df4352d6c5f58f93c508d702033f5cb2cca359ee23c412a1d3ebbf"
ENDPOINT = "fixture://jetstream_synthetic"
RUN_ID = "synthetic-recovery"


def fail(message: str) -> None:
    raise SystemExit(f"recovery refused: {message}")


def new_output(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = [Path(p) for p in ("/etc", "/opt", "/root", "/srv", "/usr", "/var")]
    if resolved.exists():
        fail(f"output already exists: {resolved}")
    if resolved == Path("/") or any(resolved == item or item in resolved.parents for item in forbidden):
        fail(f"output is under a protected live-path prefix: {resolved}")
    if not resolved.parent.is_dir():
        fail(f"output parent does not exist: {resolved.parent}")
    resolved.mkdir()
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new directory for campaign-owned recovery artifacts")
    args = parser.parse_args()
    output = new_output(args.output)
    if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != FIXTURE_SHA256:
        fail("fixed fixture digest does not match the reviewed fixture")

    with tempfile.TemporaryDirectory(prefix="working-", dir=output) as temporary:
        work = Path(temporary)
        primary = work / "primary.sqlite"
        conn = db.connect(primary)
        db.init_db(conn)
        db.start_run(conn, RUN_ID, ENDPOINT, COLLECTOR_VERSION, 60,
                     "2023-11-14T22:28:00Z", None, None)
        accumulator = Accumulator(RUN_ID, bucket_width=60)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                envelope = json.loads(line)
                classified = classify(envelope.get("event", envelope))
                if classified is not None:
                    accumulator.observe(classified)
        accumulator.close_for_shutdown()
        windows = accumulator.take_closed()
        cursor = Accumulator.commit_cursor_for(windows)
        db.flush_windows(conn, RUN_ID, ENDPOINT, windows, cursor)
        db.end_run(conn, RUN_ID, "2023-11-14T22:29:00Z", "fixture_complete",
                   accumulator.first_event_us, accumulator.last_event_us)
        report.generate_report(conn, work / "primary-report", run_ids=[RUN_ID],
                               now=datetime(2023, 11, 14, 22, 29, tzinfo=timezone.utc))
        backup_path = output / "backup.sqlite"
        backup = sqlite3.connect(backup_path)
        conn.backup(backup)
        backup.close()
        conn.close()
        restored_path = output / "restored.sqlite"
        source = sqlite3.connect(backup_path)
        restored = sqlite3.connect(restored_path)
        source.backup(restored)
        source.close()
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        restored.close()
        restored_conn = db.connect(restored_path)
        persisted_cursor = db.get_cursor(restored_conn, ENDPOINT)
        posts = restored_conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM bucket WHERE metric='post.create'"
        ).fetchone()[0]
        restored_report = output / "report"
        report.generate_report(restored_conn, restored_report, run_ids=[RUN_ID],
                               now=datetime(2023, 11, 14, 22, 29, tzinfo=timezone.utc))
        restored_conn.close()

    startup = subprocess.run(
        [sys.executable, "-m", "weatherwatch.cli", "--db", str(restored_path),
         "status", "--json", "--report-dir", str(restored_report)],
        check=False, text=True, capture_output=True)
    try:
        status = json.loads(startup.stdout)
    except json.JSONDecodeError as exc:
        fail(f"installed CLI did not emit JSON status: {exc}; stderr={startup.stderr!r}")
    assertions = {
        "backup_restore_integrity": integrity == "ok",
        "cursor_continuity": persisted_cursor == 1700000933000000,
        "meaningful_post_create_aggregate": posts == 12,
        "startup_exit_code": startup.returncode == 0,
        "status_schema": status.get("schema") == "project.ops.status/v1",
    }
    receipt = {
        "schema": "weatherwatch.synthetic-application-recovery/v1",
        "fixture": str(FIXTURE.relative_to(FIXTURE.parents[1])),
        "fixture_sha256": FIXTURE_SHA256,
        "interpreter": sys.version.split()[0],
        "assertions": assertions,
        "observed": {"cursor": persisted_cursor, "post_create": posts,
                     "integrity_check": integrity, "status_state": status.get("state")},
        "passed": all(assertions.values()),
        "scope": "synthetic application/data recovery; not source reconstruction or production rollback",
    }
    (output / "result.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
