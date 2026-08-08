"""weatherwatch CLI.

    weatherwatch collect [--duration 30m] [--endpoint ...]
    weatherwatch runs
    weatherwatch stats [--run RUN_ID]
    weatherwatch series post.create [--run RUN_ID]
    weatherwatch ratios
    weatherwatch correlate post.create.quote block.create
    weatherwatch report --output DIR

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

from . import COLLECTOR_VERSION, db, derive, query, read, timeutil
from .accumulator import DEFAULT_BUCKET_WIDTH_S
from .collector import DEFAULT_ENDPOINT, KNOWN_ENDPOINTS, Collector


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _resolve_runs(conn, run_id: str | None) -> list[str]:
    """Named run, or the most recent compatible sequence on one endpoint."""
    if run_id:
        return [run_id]
    latest = query.latest_run_id(conn)
    if latest is None:
        raise SystemExit("no observation runs recorded")
    endpoint = query.run_summary(conn, latest).endpoint
    return query.compatible_runs(conn, endpoint)


# --- collect ---------------------------------------------------------------

async def _run_collect(args) -> int:
    endpoint = (
        KNOWN_ENDPOINTS.get(args.endpoint, args.endpoint)
        if args.endpoint else DEFAULT_ENDPOINT
    )
    duration = timeutil.parse_duration(args.duration) if args.duration else None

    conn = db.connect(args.db)
    db.init_db(conn)
    collector = Collector(
        conn=conn, endpoint=endpoint, duration_s=duration,
        bucket_width=args.bucket_width,
        checkpoint_path=Path(args.db).parent / "baseline_checkpoint.json",
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: collector.stop(f"signal_{s.name.lower()}"))
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


# --- read ------------------------------------------------------------------

def _cmd_runs(args) -> int:
    conn = db.connect(args.db)
    db.init_db(conn)
    summaries = query.list_run_summaries(conn, limit=args.limit)
    if not summaries:
        print("no observation runs recorded")
        return 0
    for r in summaries:
        obs = r.observed_duration_us / 1e6
        nom = r.nominal_duration_us / 1e6
        span = f"{r.data_span_s:.0f}s" if r.data_span_s else "—"
        print(f"{r.run_id}  [{r.status}]  {r.endpoint}")
        print(f"    started={r.started_at[:19]}  ended={(r.ended_at or 'OPEN')[:19]}"
              f"  stop={r.stop_reason or '-'}  v{r.collector_version}")
        print(f"    windows={r.windows} partial={r.partial_windows} "
              f"empty={r.empty_windows} degraded={r.degraded_windows} "
              f"lagged={r.lagged_windows} seams={r.seam_windows}")
        print(f"    observed={obs:.1f}s of {nom:.0f}s nominal  data_span={span}"
              f"  gaps={r.gap_us / 1e6:.2f}s  reconnects={r.reconnects}"
              f"  events={r.events:,}"
              + ("  [resumed from cursor]" if r.replayed else ""))
    conn.close()
    return 0


def _cmd_stats(args) -> int:
    conn = db.connect(args.db)
    run_ids = _resolve_runs(conn, args.run)
    read.assert_summable(conn, run_ids)
    runs = [query.run_summary(conn, r) for r in run_ids]
    totals = query.total_events_series(conn, run_ids)
    obs = totals.observed_seconds

    print(f"source     {runs[0].endpoint}")
    print(f"runs       {len(runs)} ({', '.join(r.run_id[-6:] for r in runs)})")
    print(f"observed   {obs:.1f}s across {len(totals.observed_points)} windows"
          f"  ({len(totals.points) - len(totals.observed_points)} unobserved)")
    print(f"events     {totals.total:,}  ({totals.mean_rate:.1f}/s)"
          if totals.mean_rate else "events     none")
    print()
    print(f"{'metric':<36}{'total':>12}{'per_sec':>10}")
    for metric, total in query.metric_totals(conn, run_ids).items():
        rate = total / obs if obs else 0
        print(f"{metric:<36}{total:>12,}{rate:>10.3f}")
    conn.close()
    return 0


def _cmd_series(args) -> int:
    conn = db.connect(args.db)
    run_ids = _resolve_runs(conn, args.run)
    s = query.series(conn, run_ids, args.metric)
    if args.json:
        print(json.dumps({
            "metric": s.metric, "endpoint": s.endpoint,
            "run_ids": list(s.run_ids), "bucket_width": s.bucket_width,
            "points": [
                {"bucket_start": p.bucket_start, "count": p.count,
                 "rate": p.rate, "quality": p.quality,
                 "flags": sorted(p.flags),
                 "observed_duration_us": p.observed_duration_us}
                for p in s.points
            ],
        }, indent=2))
        conn.close()
        return 0

    deps = derive.rolling_departures(s)
    print(f"{s.metric}  @ {s.endpoint}")
    print(f"{'window':<22}{'count':>9}{'per_sec':>10}{'z':>8}  "
          f"{'condition':<11}quality")
    for p, d in zip(s.points, deps):
        when = timeutil.us_to_iso(p.bucket_start * 1_000_000)[11:19]
        if not p.observed:
            print(f"{when:<22}{'—':>9}{'—':>10}{'—':>8}  "
                  f"{'unobserved':<11}(nobody watching — not zero)")
            continue
        print(f"{when:<22}{p.count:>9,}{p.rate:>10.3f}"
              f"{(f'{d.z:.2f}' if d.z is not None else '—'):>8}  "
              f"{d.condition:<11}{p.quality}")
    conn.close()
    return 0


def _cmd_ratios(args) -> int:
    conn = db.connect(args.db)
    run_ids = _resolve_runs(conn, args.run)
    cache: dict[str, object] = {}

    def get(m):
        if m not in cache:
            cache[m] = query.series(conn, run_ids, m)
        return cache[m]

    print(f"{'ratio':<24}{'overall':>10}{'min':>10}{'max':>10}{'windows':>9}")
    for label, num, den in derive.STANDARD_RATIOS:
        a, b = get(num), get(den)
        pts = derive.ratio_series(a, b)
        vals = [p.value for p in pts if p.value is not None]
        overall = derive.ratio(a.total, b.total)
        print(f"{label:<24}"
              f"{(f'{overall:.4f}' if overall is not None else '—'):>10}"
              f"{(f'{min(vals):.4f}' if vals else '—'):>10}"
              f"{(f'{max(vals):.4f}' if vals else '—'):>10}"
              f"{len(vals):>9}")
    conn.close()
    return 0


def _cmd_correlate(args) -> int:
    conn = db.connect(args.db)
    run_ids = _resolve_runs(conn, args.run)
    a = query.series(conn, run_ids, args.metric_a)
    b = query.series(conn, run_ids, args.metric_b)
    result = derive.lagged_correlation(a, b, max_lag=args.max_lag)
    print(f"lagged correlation  {args.metric_a} → {args.metric_b}")
    print(f"(positive lag: does A lead B? window = {a.bucket_width}s)")
    for lag in sorted(result):
        r = result[lag]
        bar = ""
        if r is not None:
            bar = ("+" if r >= 0 else "-") * int(min(abs(r), 1.0) * 30)
        print(f"  lag {lag:+3d}  {(f'{r:+.3f}' if r is not None else '   n/a')}  {bar}")
    print("\nDescriptive only. Two metrics on one platform share every "
          "confounder there is; this licenses no causal claim.")
    conn.close()
    return 0


def _cmd_report(args) -> int:
    from . import report
    conn = db.connect(args.db)
    run_ids = [args.run] if args.run else None
    stats = report.generate_report(conn, args.output, run_ids=run_ids)
    conn.close()
    print(json.dumps(stats, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="weatherwatch")
    ap.add_argument("--db", default=str(db.DEFAULT_DB_PATH),
                    help="SQLite path. Keep on local disk — never NFS/SMB.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="run an observation session")
    p.add_argument("--endpoint", default=None,
                   help=f"URL or one of {sorted(KNOWN_ENDPOINTS)}")
    p.add_argument("--duration", default=None, help="e.g. 30m, 1h, 600s")
    p.add_argument("--bucket-width", type=int, default=DEFAULT_BUCKET_WIDTH_S)

    p = sub.add_parser("runs", help="list observation runs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=_cmd_runs)

    p = sub.add_parser("stats", help="metric totals and rates")
    p.add_argument("--run", default=None)
    p.set_defaults(fn=_cmd_stats)

    p = sub.add_parser("series", help="per-window series for one metric")
    p.add_argument("metric")
    p.add_argument("--run", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_series)

    p = sub.add_parser("ratios", help="standard derived ratios")
    p.add_argument("--run", default=None)
    p.set_defaults(fn=_cmd_ratios)

    p = sub.add_parser("correlate", help="lagged correlation between two metrics")
    p.add_argument("metric_a")
    p.add_argument("metric_b")
    p.add_argument("--run", default=None)
    p.add_argument("--max-lag", type=int, default=5)
    p.set_defaults(fn=_cmd_correlate)

    p = sub.add_parser("report", help="generate the static dashboard")
    p.add_argument("--output", required=True)
    p.add_argument("--run", default=None)
    p.set_defaults(fn=_cmd_report)

    p = sub.add_parser("version")
    p.set_defaults(fn=lambda a: (print(COLLECTOR_VERSION), 0)[1])

    args = ap.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "collect":
        return asyncio.run(_run_collect(args))
    try:
        return args.fn(args)
    except read.NotSummable as e:
        print(f"refusing to combine observations: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
