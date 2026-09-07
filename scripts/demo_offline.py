#!/usr/bin/env python3
"""Replay a bounded synthetic stream and assert its aggregate."""
from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from weatherwatch import COLLECTOR_VERSION, db, report, visibility
from weatherwatch.accumulator import Accumulator
from weatherwatch.classify import classify


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    owner = None
    if args.output is None:
        owner = tempfile.TemporaryDirectory(prefix="weatherwatch-demo-")
        root = Path(owner.name)
    else:
        root = args.output.resolve()
        root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root / "weatherwatch.sqlite")
    db.init_db(conn)
    run_id = "offline-demo"
    endpoint = "fixture://jetstream_synthetic"
    db.start_run(conn, run_id, endpoint, COLLECTOR_VERSION, 60,
                 "2023-11-14T22:28:00Z", None, None)
    accumulator = Accumulator(run_id, bucket_width=60)
    for line in Path("fixtures/jetstream_synthetic.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        envelope = json.loads(line)
        classified = classify(envelope.get("event", envelope))
        if classified is not None:
            accumulator.observe(classified)
    accumulator.close_for_shutdown()
    windows = accumulator.take_closed()
    cursor = Accumulator.commit_cursor_for(windows)
    db.flush_windows(conn, run_id, endpoint, windows, cursor)
    db.end_run(conn, run_id, "2023-11-14T22:29:00Z", "fixture_complete",
               accumulator.first_event_us, accumulator.last_event_us)
    posts = sum(window.counts.get("post.create", 0) for window in windows)
    if posts != 12:
        raise SystemExit(f"expected 12 synthetic post.create events, got {posts}")
    rendered = root / "report"
    report.generate_report(conn, rendered, run_ids=[run_id],
                           now=datetime(2023, 11, 14, 22, 29, tzinfo=timezone.utc))
    status = visibility.build_status(root / "weatherwatch.sqlite", rendered,
                                     now=datetime.now(timezone.utc))
    result = {
        "schema": "weatherwatch.offline-demo/v1",
        "post_create": posts,
        "run_id": run_id,
        "report": str(rendered / "index.html"),
        "status_schema": status["schema"],
    }
    print(json.dumps(result, sort_keys=True))
    conn.close()
    if owner is not None:
        owner.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
