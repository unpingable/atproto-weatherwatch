"""`weatherwatch social ...` — detection, inspection, seismogram.

Custody itself has no subcommand of its own: edges are captured by the
existing `weatherwatch collect` with `--social-edges`, because there is only
one Jetstream consumer in this estate and adding a second would be the exact
duplication this package exists to avoid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import db as weather_db
from .. import query, timeutil
from . import episodes, report, store
from .edges import TRACKED_ALIASES
from .envelope import envelope_to_dict, validate_envelope
from .sensors import aggregate, edge, lifecycle

DEFAULT_METRICS = [
    "block.create", "block.delete", "like.create", "repost.create",
    "follow.create", "follow.delete", "listitem.create", "post.delete",
    "account.active.false",
]


def _bounds(args) -> tuple[int | None, int | None]:
    since = until = None
    if getattr(args, "since", None):
        e = timeutil.to_epoch(args.since)
        since = int(e * 1_000_000) if e is not None else None
    if getattr(args, "until", None):
        e = timeutil.to_epoch(args.until)
        until = int(e * 1_000_000) if e is not None else None
    if getattr(args, "last", None):
        secs = timeutil.parse_duration(args.last)
        until = until or timeutil.now_us()
        since = until - int(secs * 1_000_000)
    return since, until


def cmd_detect(args) -> int:
    """Run sensors and persist sealed envelopes into the edge store."""
    since_us, until_us = _bounds(args)
    edge_conn = store.connect(args.social_db)
    store.init_db(edge_conn)
    sealed: list = []

    tiers = args.tiers.split(",") if args.tiers else ["aggregate"]

    if "aggregate" in tiers:
        wconn = weather_db.connect(args.db)
        weather_db.init_db(wconn)
        run_ids = (
            [args.run] if args.run
            else query.compatible_runs(
                wconn, args.endpoint or _default_endpoint(wconn))
        )
        if not run_ids:
            print("no observation runs to read", file=sys.stderr)
        else:
            cfg = aggregate.AggregateConfig(
                z_enter=args.z_enter, z_peak=args.z_peak,
                min_windows=args.min_windows,
                detect_lulls=not args.no_lulls,
            )
            metrics = args.metrics.split(",") if args.metrics else DEFAULT_METRICS
            try:
                sealed += episodes.run_aggregate(
                    wconn, run_ids, metrics, since_us, until_us, cfg)
            except query.QueryTooLarge as e:
                # The read layer refuses to truncate a series silently. That is
                # correct; surface it as something the operator can act on
                # rather than as a traceback.
                wconn.close()
                edge_conn.close()
                print(f"range too large: {e}\n"
                      f"Pass --last (e.g. --last 7d) or --since/--until.",
                      file=sys.stderr)
                return 2
        wconn.close()

    if "edge" in tiers or "lifecycle" in tiers:
        if since_us is None or until_us is None:
            print("edge/lifecycle tiers need --since/--until or --last",
                  file=sys.stderr)
            return 2

    if "edge" in tiers:
        cfg = edge.EdgeConfig(jaccard_threshold=args.jaccard)
        cols = (args.collections.split(",") if args.collections
                else sorted(TRACKED_ALIASES))
        try:
            sealed += episodes.run_edge(edge_conn, cols, since_us, until_us, cfg)
        except edge.ScopeTooLarge as e:
            print(f"refusing to sample: {e}", file=sys.stderr)
            return 2

    if "lifecycle" in tiers:
        sealed += episodes.run_lifecycle(
            edge_conn, since_us, until_us, lookback_s=args.lookback)

    for env, _ in sealed:
        violations = validate_envelope(env)
        if violations:
            print(f"envelope validation: {violations}", file=sys.stderr)
            return 3

    n = episodes.persist(edge_conn, sealed)
    edge_conn.close()
    print(json.dumps({"episodes": n, "tiers": tiers}, indent=2))
    return 0


def _default_endpoint(conn) -> str:
    row = conn.execute(
        "SELECT source_endpoint FROM observation_run "
        "ORDER BY started_at DESC LIMIT 1").fetchone()
    return row["source_endpoint"] if row else ""


def cmd_episodes(args) -> int:
    conn = store.connect(args.social_db)
    store.init_db(conn)
    rows = store.list_episodes(conn, args.since, args.until, args.limit)
    conn.close()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    print(f"{'start':22} {'type':34} {'mag':>9} {'band':9} episode")
    for r in rows:
        print(f"{r['ts_start']:22} {r['type']:34} {r['score']:9.3f} "
              f"{r['severity']:9} {r['subject_value'][:12]}")
    print(f"\n{len(rows)} episodes")
    return 0


def cmd_report(args) -> int:
    conn = store.connect(args.social_db)
    store.init_db(conn)
    rows = store.list_episodes(conn, args.since, args.until, args.limit)
    conn.close()

    from .envelope import DetectionEnvelope, EvidenceStub, SubjectRef
    envs = []
    for r in rows:
        d = json.loads(r["envelope_json"])
        envs.append(DetectionEnvelope(
            envelope_schema_version=d["envelope_schema_version"],
            detector_id=d["detector_id"], detector_version=d["detector_version"],
            ts_start=d["ts_start"], ts_end=d["ts_end"], window=d["window"],
            subject=SubjectRef(d["subject"]["type"], d["subject"]["value"]),
            type=d["type"], score=d["score"], severity=d["severity"],
            explain=d["explain"],
            evidence=tuple(EvidenceStub(e["kind"], e["ref"])
                           for e in d["evidence"]),
            window_fingerprint=d["window_fingerprint"],
            config_hash=d["config_hash"], receipt_hash=d["receipt_hash"],
            det_id=d["det_id"],
        ))

    pairs = episodes.co_occurrence(envs)
    meta = {
        "generated_at": timeutil.now_iso(),
        "episodes": len(envs),
        "co_occurring_pairs": len(pairs),
        "range": f"{args.since or 'all'} .. {args.until or 'all'}",
        "store": str(args.social_db),
    }
    path = report.generate(envs, pairs, meta, Path(args.output))
    print(str(path))
    return 0


def cmd_field(args) -> int:
    """Build the climatology and seal one observation per window."""
    from .field import observation as fobs
    from .field import run as frun
    from .field import viz as fviz

    since_us, until_us = _bounds(args)
    wconn = weather_db.connect(args.db)
    weather_db.init_db(wconn)
    run_ids = ([args.run] if args.run
               else query.compatible_runs(
                   wconn, args.endpoint or _default_endpoint(wconn)))
    if not run_ids:
        print("no observation runs to read", file=sys.stderr)
        return 2
    try:
        points, clim, obs, cands = frun.build_all(
            wconn, run_ids, since_us, until_us,
            endpoint=args.endpoint or _default_endpoint(wconn))
    except query.QueryTooLarge as e:
        print(f"range too large: {e}\nPass --last (e.g. --last 7d).",
              file=sys.stderr)
        return 2
    finally:
        wconn.close()

    if clim is None:
        print("no windows in range", file=sys.stderr)
        return 2

    econn = store.connect(args.social_db)
    store.init_db(econn)
    fobs.init(econn)
    now = timeutil.now_iso()
    econn.execute("BEGIN")
    try:
        fobs.save_climatology(econn, clim, now)
        n = fobs.save_observations(econn, obs, now)
        econn.execute("COMMIT")
    except Exception:
        econn.execute("ROLLBACK")
        raise

    out = {
        "observations": n,
        "climatology_id": clim.climatology_id,
        "days": clim.n_days,
        "weeks": clim.n_weeks,
        "hour_of_week_supported": clim.hour_of_week_supported,
        "support": {k: v.support for k, v in sorted(clim.quantities.items())},
    }
    from .field.climatology import candidate_summary
    cands_summary = candidate_summary(points, clim, cands)
    out["candidates"] = cands_summary
    if args.output or args.station_output:
        # Scoped to this run's climatology: an observation is only meaningful
        # against the baseline it was scored with.
        docs, total_obs = fobs.load_observations(
            econn, climatology_id=clim.climatology_id)
        cdoc = fobs.load_climatology(econn, clim.climatology_id)
        from .field.conditions import CRITERIA, STATE_LABEL, assess
        cond = assess(docs, cdoc).as_dict()
        cond["criteria_table"] = [
            (STATE_LABEL[s2], text) for s2, text in CRITERIA]
        meta = {"generated_at": now, "observations_in_store": total_obs}
        out["conditions"] = {k: cond[k] for k in
                             ("state", "headline", "confidence",
                              "persistence_windows", "as_of")}
        out["rendered_from_storage"] = len(docs)
        out["observations_in_store"] = total_obs
        if total_obs > len(docs):
            out["truncated_oldest"] = total_obs - len(docs)

    if args.output:
        path = Path(args.output)
        path.mkdir(parents=True, exist_ok=True)
        (path / "index.html").write_text(
            fviz.render_public(docs, cdoc, meta, cond), encoding="utf-8")
        out["page"] = str(path / "index.html")

    if args.station_output:
        # A SEPARATE artifact, never written into the public directory.
        # Merging an operator instrument into the visitor experience is what
        # this split exists to prevent, so it takes its own flag and path.
        from .field import baseline as fbase
        spath = Path(args.station_output)
        if args.output and spath.resolve() == Path(args.output).resolve():
            print("refusing to write the calibration surface into the public "
                  "output directory; give --station-output its own path",
                  file=sys.stderr)
            econn.close()
            return 2
        spath.mkdir(parents=True, exist_ok=True)
        (spath / "index.html").write_text(
            fviz.render_station(docs, cdoc, meta, cond), encoding="utf-8")
        (spath / "climatology.md").write_text(
            fbase.report(cdoc, docs, cands_summary, meta), encoding="utf-8")
        out["station_page"] = str(spath / "index.html")
        out["baseline_report"] = str(spath / "climatology.md")

    econn.close()
    print(json.dumps(out, indent=2))
    return 0


def cmd_custody(args) -> int:
    """Sink health: what was seen, stored, skipped and dropped."""
    conn = store.connect(args.social_db)
    store.init_db(conn)
    rows = conn.execute(
        "SELECT run_id, MAX(flush_seq) AS flushes, MAX(seen) AS seen, "
        "MAX(stored_edges) AS edges, MAX(stored_status) AS status, "
        "MAX(dropped_backpressure) AS dropped, MIN(first_event_us) AS t0, "
        "MAX(last_event_us) AS t1 FROM sink_health GROUP BY run_id "
        "ORDER BY run_id"
    ).fetchall()
    counts = conn.execute(
        "SELECT collection, op, COUNT(*) AS n FROM edge_event "
        "GROUP BY collection, op ORDER BY n DESC").fetchall()
    statuses = conn.execute(
        "SELECT status, COUNT(*) AS n FROM status_event "
        "GROUP BY status ORDER BY n DESC").fetchall()
    conn.close()
    out = {
        "runs": [dict(r) for r in rows],
        "edges": [dict(r) for r in counts],
        "status_events": [dict(r) for r in statuses],
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def register(sub, default_social_db) -> None:
    """Attach `social` subcommands to the weatherwatch parser."""
    p = sub.add_parser("social", help="social episode sensors (local only)")
    ssub = p.add_subparsers(dest="social_cmd", required=True)

    def _leaf(name, help_):
        # `--social-db` lives on every leaf rather than on the `social` group
        # so it can be written after the verb, where a reader expects it.
        q = ssub.add_parser(name, help=help_)
        q.add_argument("--social-db", default=str(default_social_db),
                       help="edge store path; a separate file from the "
                            "weather DB, with a different retention posture")
        return q

    d = _leaf("detect", "run sensors and seal episodes")
    d.add_argument("--tiers", default="aggregate",
                   help="comma list: aggregate,edge,lifecycle")
    d.add_argument("--metrics", default=None)
    d.add_argument("--collections", default=None)
    d.add_argument("--run", default=None)
    d.add_argument("--endpoint", default=None)
    d.add_argument("--since", default=None)
    d.add_argument("--until", default=None)
    d.add_argument("--last", default=None, help="e.g. 24h")
    d.add_argument("--z-enter", type=float, default=3.0)
    d.add_argument("--z-peak", type=float, default=3.0)
    d.add_argument("--min-windows", type=int, default=2)
    d.add_argument("--no-lulls", action="store_true")
    d.add_argument("--jaccard", type=float, default=0.5)
    d.add_argument("--lookback", type=int, default=lifecycle.DEFAULT_LOOKBACK_S)
    d.set_defaults(fn=cmd_detect)

    e = _leaf("episodes", "list sealed episodes")
    e.add_argument("--since", default=None)
    e.add_argument("--until", default=None)
    e.add_argument("--limit", type=int, default=200)
    e.add_argument("--json", action="store_true")
    e.set_defaults(fn=cmd_episodes)

    r = _leaf("report", "write the local seismogram")
    r.add_argument("--output", required=True)
    r.add_argument("--since", default=None)
    r.add_argument("--until", default=None)
    r.add_argument("--limit", type=int, default=2000)
    r.set_defaults(fn=cmd_report)

    c = _leaf("custody", "edge sink health and volume")
    c.set_defaults(fn=cmd_custody)

    f = _leaf("field", "build the social-weather climatology and observations")
    f.add_argument("--run", default=None)
    f.add_argument("--endpoint", default=None)
    f.add_argument("--since", default=None)
    f.add_argument("--until", default=None)
    f.add_argument("--last", default=None, help="e.g. 7d")
    f.add_argument("--output", default=None,
                   help="render the PUBLIC weather page into this directory, "
                        "from STORED observations rather than from memory")
    f.add_argument("--station-output", default=None,
                   help="render the calibration surface and the climatology "
                        "baseline report into this directory. Separate from "
                        "--output on purpose: operator instrumentation, not "
                        "published.")
    f.set_defaults(fn=cmd_field)
