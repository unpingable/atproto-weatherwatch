"""weatherwatch CLI.

    weatherwatch collect [--duration 30m] [--endpoint ...]
    weatherwatch runs
    weatherwatch stats [--run RUN_ID]
    weatherwatch series post.create [--run RUN_ID]
    weatherwatch ratios
    weatherwatch correlate post.create.quote block.create
    weatherwatch report --output DIR
    weatherwatch status [--json] [--report-dir DIR]
    weatherwatch publication-gate --report-dir DIR [--json]
    weatherwatch compose --input reduced-facts.json --rule RULE_ID
    weatherwatch compose --list-rules
    weatherwatch plc-reduce --input sequenced-export.jsonl --acquired-at TIME
    weatherwatch social detect --last 24h
    weatherwatch social report --output DIR

Local instrument. No HTTP server, no public surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import datetime as dt
from pathlib import Path

from . import COLLECTOR_VERSION, db, derive, query, read, timeutil
from .accumulator import DEFAULT_BUCKET_WIDTH_S
from .collector import DEFAULT_ENDPOINT, KNOWN_ENDPOINTS, Collector


LOG = logging.getLogger("weatherwatch.cli")

from .social.config import RECEIPT_FILENAME as SOCIAL_RECEIPT_FILENAME
from .social.config import RECEIPT_META_KEY as SOCIAL_RECEIPT_META_KEY


def _record_social_receipt(conn, cfg, run_id: str, data_dir: Path) -> dict:
    """Record whether edge custody ran, in the weather DB and on disk.

    The `meta` row holds the *published* receipt -- state, scope, horizon, no
    filesystem path -- because the report renders it and the report gets
    published. The file beside the database holds the full one.
    """
    receipt = cfg.public_receipt(run_id)
    db.set_meta(conn, SOCIAL_RECEIPT_META_KEY, json.dumps(receipt,
                                                          sort_keys=True))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / SOCIAL_RECEIPT_FILENAME).write_text(
            json.dumps(cfg.receipt(run_id), indent=2, sort_keys=True),
            encoding="utf-8")
    except OSError:
        LOG.warning("could not write %s; the meta row still holds the receipt",
                    SOCIAL_RECEIPT_FILENAME)
    return receipt


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

    # Activation is resolved before the collector exists, and the decision is
    # recorded whether it came out on or off. A flag that only records itself
    # when it is on is an advertisement, not a receipt.
    from .social.config import ConfigError, from_args as social_from_args
    try:
        social_cfg = social_from_args(args)
    except ConfigError as e:
        print(f"social sink configuration refused: {e}", file=sys.stderr)
        return 2

    social_sink = None
    if social_cfg.enabled:
        from .social.sink import SocialSink
        social_sink = SocialSink.open(
            social_cfg.db_path, run_id="pending",
            collections=social_cfg.collection_set,
            retention_us=social_cfg.retention_us,
            batch_rows=social_cfg.batch_rows,
        )

    collector = Collector(
        conn=conn, endpoint=endpoint, duration_s=duration,
        bucket_width=args.bucket_width,
        checkpoint_path=Path(args.db).parent / "baseline_checkpoint.json",
        social_sink=social_sink,
    )
    if social_sink is not None:
        # The sink shares the collector's run identity so custody and weather
        # rows from one session can be lined up without a second run table.
        social_sink.writer.run_id = collector.run_id

    # Written by weatherwatch about its own configuration -- never by the
    # social package, which must not touch this database at all. It is a
    # boolean plus scope, carries no edge and no identity, and it is written
    # on every run so the OFF case is on the record too.
    _record_social_receipt(conn, social_cfg, collector.run_id,
                           Path(args.db).parent)
    LOG.info("social edge custody %s%s",
             "ON" if social_cfg.enabled else "OFF",
             (f" -> {social_cfg.db_path} "
              f"collections={sorted(social_cfg.collections)} "
              f"retention={social_cfg.retention}")
             if social_cfg.enabled else "")

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
    stats = report.generate_report(conn, args.output, run_ids=run_ids,
                                   public_url=args.public_url,
                                   social_db=args.social_db,
                                   social_window=args.social_window)
    conn.close()
    print(json.dumps(stats, indent=2))
    return 0


def _parse_status_now(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"invalid --now timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise SystemExit("--now must include a timezone")
    return parsed


def _cmd_status(args) -> int:
    from . import visibility
    document = visibility.build_status(
        db_path=args.db, report_dir=args.report_dir,
        now=_parse_status_now(args.now))
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True))
    else:
        print(visibility.render_human(document))
    return 0


def _cmd_publication_gate(args) -> int:
    from .publication import evaluate_candidate
    result = evaluate_candidate(args.report_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"publication gate: {result['disposition']} — {result['reason']}")
        for refusal in result["refusals"]:
            suffix = f" ({refusal.get('file', '')})" if refusal.get("file") else ""
            print(f"  {refusal['kind']}{suffix}")
        print("candidate eligibility is not publication authority")
    return 0 if result["disposition"] == "PASSED" else 2


def _cmd_compose(args) -> int:
    """Compose already-reduced facts; never ingest an external source here."""
    from . import composition
    if args.list_rules:
        print(json.dumps(composition.rules_document(), indent=2, sort_keys=True))
        return 0
    if not args.input or not args.rule:
        print("compose requires --input and --rule (or --list-rules)",
              file=sys.stderr)
        return 2
    try:
        document = json.loads(Path(args.input).read_text(encoding="utf-8"))
        output = composition.compose(document, args.rule)
    except (OSError, json.JSONDecodeError) as exc:
        refusal = {
            "schema": composition.SCHEMA_REFUSAL,
            "disposition": composition.REFUSED,
            "code": "INPUT_UNREADABLE",
            "path": "input",
            "reason": f"composition input could not be read ({type(exc).__name__})",
            "rejected_value_echoed": False,
        }
        print(json.dumps(refusal, indent=2, sort_keys=True))
        return 2
    except composition.CompositionRefused as exc:
        print(json.dumps(exc.as_dict(), indent=2, sort_keys=True))
        return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["disposition"] == "COMPOSED" else 2


def _cmd_plc_reduce(args) -> int:
    """Run the one identity-aware reducer behind a non-echoing CLI boundary."""
    from . import plc_reducer
    with plc_reducer.suppress_core_dumps() as core_suppressed:
        if not core_suppressed:
            refusal = plc_reducer.PLCReductionRefused(
                "CORE_DUMP_SUPPRESSION_UNAVAILABLE", "process",
                "raw PLC input is refused unless process core dumps are disabled")
            print(json.dumps(refusal.as_dict(), indent=2, sort_keys=True))
            return 2
        with plc_reducer.controlled_termination_signals() as signals_controlled:
            if not signals_controlled:
                refusal = plc_reducer.PLCReductionRefused(
                    "SIGNAL_CUSTODY_UNAVAILABLE", "process",
                    "raw PLC input is refused unless termination signals can be controlled")
                print(json.dumps(refusal.as_dict(), indent=2, sort_keys=True))
                return 2
            try:
                acquired_at = plc_reducer.parse_timestamp(
                    args.acquired_at, "acquired_at")
                seconds = timeutil.parse_duration(args.window)
                if not float(seconds).is_integer():
                    raise plc_reducer.PLCReductionRefused(
                        "UNSAFE_WINDOW_POLICY", "policy.window_seconds",
                        "PLC publication windows must be whole non-overlapping UTC weeks")
                output = plc_reducer.reduce_path(
                    args.input, acquired_at=acquired_at,
                    window_seconds=int(seconds),
                    disclosure_count=args.min_disclosure_count,
                    core_dump_suppressed=True)
            except plc_reducer.PLCReductionRefused as exc:
                exit_code = getattr(exc, "exit_code", 2)
                payload = exc.as_dict()
                del exc  # discard chained parser frames before restoring custody
                print(json.dumps(payload, indent=2, sort_keys=True))
                return exit_code
            except Exception as exc:  # raw input must not escape through a traceback
                exception_class = type(exc).__name__
                del exc  # discard traceback and raw reducer locals under custody
                refusal = plc_reducer.PLCReductionRefused(
                    "REDUCER_FAILED", "reducer",
                    f"PLC reducer failed without exposing input ({exception_class})")
                print(json.dumps(refusal.as_dict(), indent=2, sort_keys=True))
                return 2
            document = output["bundle"] if args.bundle_only else output
            print(json.dumps(document, indent=2, sort_keys=True))
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
    # Edge custody is opt-in and off by default. Turning it on changes this
    # process's retention posture -- see src/weatherwatch/social/BOUNDARIES.md.
    p.add_argument("--social-edges", action="store_true",
                   help="ALSO retain actor->subject edges to a separate store. "
                        "Changes the retention posture of this run. Equivalent "
                        "to WW_SOCIAL_EDGES=1; the flag wins over the env var.")
    p.add_argument("--social-db", default=None,
                   help="edge store path (env WW_SOCIAL_DB)")
    p.add_argument("--social-collections", default=None,
                   help="comma list of collections to retain (env "
                        "WW_SOCIAL_COLLECTIONS). likes/reposts are high "
                        "volume: ~216/s and ~34/s as observed.")
    p.add_argument("--social-retention", default=None,
                   help="prune edges older than this on flush "
                        "(env WW_SOCIAL_RETENTION)")

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
    p.add_argument("--social-db", default=os.environ.get("WW_SOCIAL_DB"),
                   help="episode store to project into the report's social "
                        "section. Only aggregate-tier episodes are published; "
                        "they are derived from the identity-free counters and "
                        "carry no actor or target.")
    p.add_argument("--social-window", default=os.environ.get(
                       "WW_SOCIAL_WINDOW", "48h"),
                   help="how far back the social section reaches (default "
                        "48h). The window is stated on the page; it is not a "
                        "silent cap.")
    p.add_argument("--public-url", default=os.environ.get("WW_PUBLIC_URL"),
                   help="canonical URL of the published report. Only when set "
                        "are share-card meta tags emitted; without it the page "
                        "is entirely self-contained.")
    p.set_defaults(fn=_cmd_report)

    p = sub.add_parser(
        "status", help="current local visibility (human or versioned JSON)")
    p.add_argument("--report-dir", default="build/report",
                   help="rendered candidate directory (default build/report)")
    p.add_argument("--json", action="store_true",
                   help="emit project.ops.status/v1 JSON")
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p.set_defaults(fn=_cmd_status)

    p = sub.add_parser(
        "publication-gate", help="evaluate local candidate publication eligibility")
    p.add_argument("--report-dir", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=_cmd_publication_gate)

    p = sub.add_parser(
        "compose", help="clock-only composition of already-reduced facts")
    p.add_argument("--input", default=None,
                   help="weatherwatch.composition.bundle/v1 JSON")
    p.add_argument("--rule", default=None,
                   help="installed explicit semantic composition rule")
    p.add_argument("--list-rules", action="store_true",
                   help="emit installed rules and their interpretation bounds")
    p.set_defaults(fn=_cmd_compose)

    p = sub.add_parser(
        "plc-reduce", help="reduce sequenced PLC export into identity-free facts")
    p.add_argument("--input", required=True,
                   help="local sequenced PLC /export JSONL acquisition")
    p.add_argument("--acquired-at", required=True,
                   help="when this exact source artifact was acquired (ISO-8601)")
    p.add_argument("--window", default="7d",
                   help="non-overlapping UTC window; minimum/multiple 7d")
    p.add_argument("--min-disclosure-count", type=int, default=10,
                   help="per fact, zero through N-1 are suppressed; minimum 10")
    p.add_argument("--bundle-only", action="store_true",
                   help="emit only the composition bundle, without reducer receipt")
    p.set_defaults(fn=_cmd_plc_reduce)

    from .social.cli import register as _register_social
    from .social.store import DEFAULT_EDGE_DB_PATH
    _register_social(sub, DEFAULT_EDGE_DB_PATH)

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
