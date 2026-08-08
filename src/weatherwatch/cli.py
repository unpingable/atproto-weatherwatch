"""weatherwatch CLI.

    weatherwatch collect
    weatherwatch collect --duration 30m
    weatherwatch collect --endpoint jetstream2.us-east
    weatherwatch runs
    weatherwatch show <run_id>

Local instrument. No HTTP server, no public surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from . import COLLECTOR_VERSION, db, read, timeutil
from .accumulator import DEFAULT_BUCKET_WIDTH_S
from .collector import DEFAULT_ENDPOINT, KNOWN_ENDPOINTS, Collector


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _run_collect(args) -> int:
    endpoint = (
        KNOWN_ENDPOINTS.get(args.endpoint, args.endpoint)
        if args.endpoint else DEFAULT_ENDPOINT
    )
    duration = timeutil.parse_duration(args.duration) if args.duration else None

    conn = db.connect(args.db)
    db.init_db(conn)

    collector = Collector(
        conn=conn,
        endpoint=endpoint,
        duration_s=duration,
        bucket_width=args.bucket_width,
        checkpoint_path=Path(args.db).parent / "baseline_checkpoint.json",
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: collector.stop(f"signal_{s.name.lower()}")
            )
        except NotImplementedError:
            pass

    print(f"run_id={collector.run_id} endpoint={endpoint} "
          f"duration={args.duration or 'unbounded'} width={args.bucket_width}s",
          file=sys.stderr)
    try:
        await collector.run()
    finally:
        conn.close()
    print(f"run_id={collector.run_id}")
    return 0


def _cmd_runs(args) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    rows = db.list_runs(conn, limit=args.limit)
    if not rows:
        print("no observation runs recorded")
        return 0
    for r in rows:
        cov = read.run_coverage(conn, r["run_id"])
        observed = (cov["observed_duration_us"] or 0) / 1_000_000 if cov else 0
        nominal = (cov["nominal_duration_us"] or 0) / 1_000_000 if cov else 0
        print(
            f"{r['run_id']}  {r['source_endpoint']}\n"
            f"    started={r['started_at']}  ended={r['ended_at'] or 'OPEN'}"
            f"  stop={r['stop_reason'] or '-'}\n"
            f"    windows={cov['windows'] if cov else 0}"
            f" partial={cov['partial_windows'] if cov else 0}"
            f" empty={cov['empty_windows'] if cov else 0}"
            f" events={cov['events'] if cov else 0}\n"
            f"    observed={observed:.1f}s of {nominal:.1f}s nominal"
            f"  gaps={(cov['gap_us'] or 0) / 1_000_000 if cov else 0:.2f}s"
            f"  reconnects={cov['reconnects'] if cov else 0}"
            f"  seams={cov['seams'] if cov else 0}"
        )
    conn.close()
    return 0


def _cmd_show(args) -> int:
    conn = db.connect(args.db)
    run = db.get_run(conn, args.run_id)
    if run is None:
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    cov = read.run_coverage(conn, args.run_id)
    out = {
        "run": dict(run),
        "coverage": dict(cov) if cov else {},
        "totals": {r["metric"]: r["total"] for r in read.run_totals(conn, args.run_id)},
        "claim": (
            "Aggregate activity observed from this Jetstream source during "
            "this observation interval."
        ),
        "note": (
            "Counts describe what this endpoint delivered. They are not a "
            "claim about the network's total activity. Monotonic stream time "
            "is not evidence of complete observation."
        ),
    }
    print(json.dumps(out, indent=2, default=str))
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="weatherwatch")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH),
                    help="SQLite path. Keep on local disk — never NFS/SMB.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="run an observation session")
    p.add_argument("--endpoint", default=None,
                   help=f"URL or one of {sorted(KNOWN_ENDPOINTS)}. "
                        f"Default {DEFAULT_ENDPOINT}")
    p.add_argument("--duration", default=None, help="e.g. 30m, 1h, 600s")
    p.add_argument("--bucket-width", type=int, default=DEFAULT_BUCKET_WIDTH_S)
    p.set_defaults(fn=None)

    p = sub.add_parser("runs", help="list observation runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=_cmd_runs)

    p = sub.add_parser("show", help="summarise one run")
    p.add_argument("run_id")
    p.set_defaults(fn=_cmd_show)

    p = sub.add_parser("version")
    p.set_defaults(fn=lambda a: (print(COLLECTOR_VERSION), 0)[1])

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "collect":
        return asyncio.run(_run_collect(args))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
