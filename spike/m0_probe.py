"""M0 verification spike. Disposable.

Subcommands:
  survey   — unfiltered Jetstream survey + scrubbed fixture capture
  cursor   — cursor boundary semantics (is `cursor=T` inclusive or exclusive?)
  control  — filtered-vs-unfiltered, retention horizon, slow consumer,
             cross-instance comparison

Deliberately has no queue, no writer thread, no database, no resolver.
Counters live in dicts; raw events are never written to disk.

Usage:
  python3 spike/m0_probe.py survey --seconds 600
  python3 spike/m0_probe.py cursor --trials 5
  python3 spike/m0_probe.py control
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrub import Scrubber  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
MEASUREMENTS = ROOT / "measurements"

INSTANCES = {
    "jetstream2.us-east": "wss://jetstream2.us-east.bsky.network/subscribe",
    "jetstream1.us-east": "wss://jetstream1.us-east.bsky.network/subscribe",
    "jetstream1.us-west": "wss://jetstream1.us-west.bsky.network/subscribe",
}
DEFAULT_URL = INSTANCES["jetstream2.us-east"]

KNOWN_KINDS = {"commit", "identity", "account"}
KNOWN_OPS = {"create", "update", "delete"}

WATCHED_COLLECTIONS = [
    "app.bsky.feed.post",
    "app.bsky.feed.like",
    "app.bsky.feed.repost",
    "app.bsky.graph.follow",
    "app.bsky.graph.block",
    "app.bsky.graph.listitem",
    "app.bsky.actor.profile",
]

# Fixture capture budget. Split so a 10-minute run cannot spend the whole
# corpus on long-tail third-party lexicons in the first 20 seconds: the
# classifier-relevant collections and identity/account events get a reserved
# allowance, everything else shares a small remainder.
MAX_FIXTURES_PER_SHAPE = 2
MAX_FIXTURES_PRIORITY = 300   # watched collections + identity + account
MAX_FIXTURES_LONGTAIL = 60    # everything else, 1 per shape


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[i]


def build_url(base: str, collections=None, cursor=None) -> str:
    params = []
    for c in (collections or []):
        params.append(f"wantedCollections={c}")
    if cursor is not None:
        params.append(f"cursor={int(cursor)}")
    return base + ("?" + "&".join(params) if params else "")


# ---------------------------------------------------------------------------
# survey
# ---------------------------------------------------------------------------

class Survey:
    def __init__(self):
        self.msgs = 0
        self.approx_chars = 0
        self.parse_failures = 0
        self.missing_time_us = 0
        self.kinds = Counter()
        self.unknown_kinds = Counter()
        self.collections = Counter()
        self.operations = Counter()
        self.coll_op = Counter()
        self.unknown_ops = Counter()

        # time_us monotonicity
        self.prev_time_us = None
        self.tu_increasing = 0
        self.tu_equal = 0
        self.tu_decreasing = 0
        self.tu_max_backward_us = 0

        self.lag_samples = []
        self.per_second = Counter()

        # delete commit shape
        self.delete_total = 0
        self.delete_with_record_key = 0
        self.delete_missing_collection = 0
        self.delete_missing_rkey = 0
        self.delete_with_cid = 0
        self.delete_with_rev = 0

        # post structure
        self.posts = 0
        self.posts_with_reply = 0
        self.posts_with_facets = 0
        self.posts_with_embed = 0
        self.embed_types = Counter()
        self.embed_missing_type = 0
        self.rwm_media_types = Counter()
        self.rwm_media_missing_type = 0
        self.facet_feature_types = Counter()
        self.facet_feature_missing_type = 0

        # identity / account shapes
        self.identity_keys = Counter()
        self.account_keys = Counter()
        self.account_active = Counter()
        self.account_status = Counter()

        # commit envelope key presence
        self.commit_keys = Counter()
        self.envelope_keys = Counter()

        # fixtures
        self.scrubber = Scrubber()
        self.shape_counts = Counter()
        self.fixtures = []
        self.fixtures_priority = 0
        self.fixtures_longtail = 0

    # -- fixture shape signature -------------------------------------------

    @staticmethod
    def shape_sig(msg: dict) -> str:
        kind = msg.get("kind")
        if kind != "commit":
            inner = msg.get(kind) if isinstance(msg.get(kind), dict) else {}
            return f"{kind}|keys={','.join(sorted(inner.keys()))}"
        c = msg.get("commit") or {}
        coll = c.get("collection")
        op = c.get("operation")
        rec = c.get("record")
        if not isinstance(rec, dict):
            return f"commit|{coll}|{op}|norecord"
        if coll == "app.bsky.feed.post":
            embed = rec.get("embed") or {}
            et = embed.get("$type", "none") if isinstance(embed, dict) else "none"
            return (
                f"commit|{coll}|{op}|reply={int('reply' in rec)}"
                f"|embed={et}|facets={int('facets' in rec)}"
                f"|labels={int('labels' in rec)}|langs={int('langs' in rec)}"
            )
        return f"commit|{coll}|{op}|keys={','.join(sorted(rec.keys()))}"

    def maybe_capture(self, msg: dict) -> None:
        kind = msg.get("kind")
        coll = (msg.get("commit") or {}).get("collection") if kind == "commit" else None
        priority = (kind in ("identity", "account")) or (coll in WATCHED_COLLECTIONS)

        if priority:
            if self.fixtures_priority >= MAX_FIXTURES_PRIORITY:
                return
            per_shape_cap = MAX_FIXTURES_PER_SHAPE
        else:
            if self.fixtures_longtail >= MAX_FIXTURES_LONGTAIL:
                return
            per_shape_cap = 1

        sig = self.shape_sig(msg)
        if self.shape_counts[sig] >= per_shape_cap:
            return
        self.shape_counts[sig] += 1
        if priority:
            self.fixtures_priority += 1
        else:
            self.fixtures_longtail += 1
        # Scrub immediately; the raw dict is dropped when this frame returns.
        self.fixtures.append({"_shape": sig, "event": self.scrubber.scrub_envelope(msg)})

    # -- per-message accounting --------------------------------------------

    def observe(self, raw: str, wall_us: float) -> None:
        self.msgs += 1
        self.approx_chars += len(raw)
        try:
            msg = json.loads(raw)
        except Exception:
            self.parse_failures += 1
            return
        if not isinstance(msg, dict):
            self.parse_failures += 1
            return

        for k in msg.keys():
            self.envelope_keys[k] += 1

        kind = msg.get("kind")
        self.kinds[str(kind)] += 1
        if kind not in KNOWN_KINDS:
            self.unknown_kinds[str(kind)] += 1

        tu = msg.get("time_us")
        if not isinstance(tu, int):
            self.missing_time_us += 1
        else:
            if self.prev_time_us is not None:
                if tu > self.prev_time_us:
                    self.tu_increasing += 1
                elif tu == self.prev_time_us:
                    self.tu_equal += 1
                else:
                    self.tu_decreasing += 1
                    self.tu_max_backward_us = max(
                        self.tu_max_backward_us, self.prev_time_us - tu
                    )
            self.prev_time_us = tu
            self.lag_samples.append((wall_us - tu) / 1_000_000.0)
        self.per_second[int(wall_us // 1_000_000)] += 1

        if kind == "identity":
            inner = msg.get("identity")
            if isinstance(inner, dict):
                for k in inner:
                    self.identity_keys[k] += 1
        elif kind == "account":
            inner = msg.get("account")
            if isinstance(inner, dict):
                for k in inner:
                    self.account_keys[k] += 1
                self.account_active[str(inner.get("active"))] += 1
                if "status" in inner:
                    self.account_status[str(inner.get("status"))] += 1
        elif kind == "commit":
            c = msg.get("commit")
            if isinstance(c, dict):
                for k in c:
                    self.commit_keys[k] += 1
                coll = c.get("collection")
                op = c.get("operation")
                self.collections[str(coll)] += 1
                self.operations[str(op)] += 1
                self.coll_op[f"{coll}|{op}"] += 1
                if op not in KNOWN_OPS:
                    self.unknown_ops[str(op)] += 1
                if op == "delete":
                    self.delete_total += 1
                    if "record" in c:
                        self.delete_with_record_key += 1
                    if not coll:
                        self.delete_missing_collection += 1
                    if not c.get("rkey"):
                        self.delete_missing_rkey += 1
                    if c.get("cid"):
                        self.delete_with_cid += 1
                    if c.get("rev"):
                        self.delete_with_rev += 1
                if coll == "app.bsky.feed.post" and isinstance(c.get("record"), dict):
                    self._observe_post(c["record"])

        self.maybe_capture(msg)

    def _observe_post(self, rec: dict) -> None:
        self.posts += 1
        if "reply" in rec:
            self.posts_with_reply += 1
        if "facets" in rec:
            self.posts_with_facets += 1
            for f in rec.get("facets") or []:
                if not isinstance(f, dict):
                    continue
                for feat in f.get("features") or []:
                    if not isinstance(feat, dict):
                        continue
                    t = feat.get("$type")
                    if t is None:
                        self.facet_feature_missing_type += 1
                    else:
                        self.facet_feature_types[t] += 1
        embed = rec.get("embed")
        if isinstance(embed, dict):
            self.posts_with_embed += 1
            t = embed.get("$type")
            if t is None:
                self.embed_missing_type += 1
            else:
                self.embed_types[t] += 1
            if t == "app.bsky.embed.recordWithMedia":
                media = embed.get("media")
                if isinstance(media, dict):
                    mt = media.get("$type")
                    if mt is None:
                        self.rwm_media_missing_type += 1
                    else:
                        self.rwm_media_types[mt] += 1

    # -- report -------------------------------------------------------------

    def report(self, elapsed_s: float, url: str) -> dict:
        secs = sorted(self.per_second.keys())
        # drop first and last partial seconds
        eps_series = [self.per_second[s] for s in secs[1:-1]] if len(secs) > 2 else []
        return {
            "url": url,
            "elapsed_s": round(elapsed_s, 1),
            "messages_total": self.msgs,
            "approx_chars_total": self.approx_chars,
            "mean_events_per_sec": round(self.msgs / elapsed_s, 1) if elapsed_s else None,
            "approx_bytes_per_sec": round(self.approx_chars / elapsed_s) if elapsed_s else None,
            "approx_bytes_per_event": round(self.approx_chars / self.msgs) if self.msgs else None,
            "eps_p50": pct(eps_series, 50),
            "eps_p95": pct(eps_series, 95),
            "eps_max": max(eps_series) if eps_series else None,
            "eps_min": min(eps_series) if eps_series else None,
            "parse_failures": self.parse_failures,
            "missing_time_us": self.missing_time_us,
            "kinds": dict(self.kinds),
            "unknown_kinds": dict(self.unknown_kinds),
            "operations": dict(self.operations),
            "unknown_operations": dict(self.unknown_ops),
            "envelope_keys": dict(self.envelope_keys),
            "commit_keys": dict(self.commit_keys),
            "distinct_collections": len(self.collections),
            "collections_top50": dict(self.collections.most_common(50)),
            "collection_operation_top60": dict(self.coll_op.most_common(60)),
            "watched_collections_present": {
                c: self.collections.get(c, 0) for c in WATCHED_COLLECTIONS
            },
            "time_us_monotonicity": {
                "increasing": self.tu_increasing,
                "equal": self.tu_equal,
                "decreasing": self.tu_decreasing,
                "max_backward_us": self.tu_max_backward_us,
            },
            "lag_s": {
                "p50": round(pct(self.lag_samples, 50) or 0, 3),
                "p95": round(pct(self.lag_samples, 95) or 0, 3),
                "max": round(max(self.lag_samples), 3) if self.lag_samples else None,
                "min": round(min(self.lag_samples), 3) if self.lag_samples else None,
            },
            "delete_commit_shape": {
                "deletes_total": self.delete_total,
                "with_record_key": self.delete_with_record_key,
                "missing_collection": self.delete_missing_collection,
                "missing_rkey": self.delete_missing_rkey,
                "with_cid": self.delete_with_cid,
                "with_rev": self.delete_with_rev,
            },
            "post_structure": {
                "posts": self.posts,
                "with_reply": self.posts_with_reply,
                "with_facets": self.posts_with_facets,
                "with_embed": self.posts_with_embed,
                "embed_types": dict(self.embed_types),
                "embed_missing_dollar_type": self.embed_missing_type,
                "recordWithMedia_media_types": dict(self.rwm_media_types),
                "recordWithMedia_media_missing_dollar_type": self.rwm_media_missing_type,
                "facet_feature_types": dict(self.facet_feature_types),
                "facet_feature_missing_dollar_type": self.facet_feature_missing_type,
            },
            "identity_event_keys": dict(self.identity_keys),
            "account_event_keys": dict(self.account_keys),
            "account_active_values": dict(self.account_active),
            "account_status_values": dict(self.account_status),
            "fixture_shapes_captured": len(self.shape_counts),
            "fixtures_written": len(self.fixtures),
            "scrubber_dropped_keys": dict(
                sorted(self.scrubber.dropped_keys.items(), key=lambda kv: -kv[1])
            ),
            "scrubber_kept_keys": dict(
                sorted(self.scrubber.kept_keys.items(), key=lambda kv: -kv[1])
            ),
        }


async def cmd_survey(args):
    s = Survey()
    url = build_url(args.url)
    print(f"[survey] connecting unfiltered: {url}", flush=True)
    deadline = time.monotonic() + args.seconds
    started = time.monotonic()
    reconnects = 0
    while time.monotonic() < deadline:
        try:
            async with websockets.connect(
                url, max_size=10 * 1024 * 1024, ping_interval=30, ping_timeout=10
            ) as ws:
                print(f"[survey] connected ({reconnects} reconnects so far)", flush=True)
                last_log = time.monotonic()
                async for raw in ws:
                    s.observe(raw, time.time() * 1_000_000)
                    now = time.monotonic()
                    if now >= deadline:
                        break
                    if now - last_log >= 60:
                        print(
                            f"[survey] t={int(now - started)}s msgs={s.msgs} "
                            f"eps~{s.msgs / (now - started):.0f} "
                            f"colls={len(s.collections)} fixtures={len(s.fixtures)}",
                            flush=True,
                        )
                        last_log = now
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reconnects += 1
            print(f"[survey] connection error ({type(e).__name__}: {e}); retry in 3s", flush=True)
            await asyncio.sleep(3)

    elapsed = time.monotonic() - started
    rep = s.report(elapsed, url)
    rep["reconnects_during_survey"] = reconnects

    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (MEASUREMENTS / "survey.json").write_text(json.dumps(rep, indent=2, sort_keys=True))
    with (FIXTURES / "jetstream_shapes.jsonl").open("w") as fh:
        for f in s.fixtures:
            fh.write(json.dumps(f, sort_keys=True) + "\n")
    print(json.dumps(rep, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# cursor boundary semantics
# ---------------------------------------------------------------------------

def _ident_key(msg: dict) -> str:
    """Transient in-memory identity for replay comparison only. Never stored."""
    c = msg.get("commit") or {}
    return "|".join(str(x) for x in (
        msg.get("kind"), msg.get("did"), c.get("collection"),
        c.get("rkey"), c.get("rev"), c.get("operation"),
    ))


async def _collect(url: str, n: int, timeout: float = 45.0):
    """Return list of (time_us, ident_key) for the first n messages."""
    out = []
    async with websockets.connect(
        url, max_size=10 * 1024 * 1024, ping_interval=30, ping_timeout=10
    ) as ws:
        deadline = time.monotonic() + timeout
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            tu = msg.get("time_us")
            if not isinstance(tu, int):
                continue
            out.append((tu, _ident_key(msg)))
            if len(out) >= n or time.monotonic() > deadline:
                break
    return out


async def cmd_cursor(args):
    """Is `cursor=T` inclusive (T replayed) or exclusive (resume after T)?

    Procedure per trial:
      1. connect fresh, collect M events
      2. pick T = time_us of the event at index PIVOT
      3. close cleanly
      4. reconnect with cursor=T, collect a few events
      5. classify: is the event at T re-delivered?
    """
    results = []
    # Filter to one mid-rate collection so a single time_us maps to few events
    # and the boundary question stays crisp.
    url_base = build_url(args.url, collections=["app.bsky.feed.post"])
    for trial in range(args.trials):
        try:
            first = await _collect(url_base, args.collect)
            if len(first) < args.pivot + 5:
                results.append({"trial": trial, "error": "insufficient events"})
                continue
            pivot_tu, pivot_id = first[args.pivot]
            # what came immediately after the pivot, in-stream
            next_tu, next_id = first[args.pivot + 1]

            await asyncio.sleep(1.0)
            resumed = await _collect(build_url(args.url, ["app.bsky.feed.post"], cursor=pivot_tu), 8)
            if not resumed:
                results.append({"trial": trial, "error": "no events on resume"})
                continue
            r_tu, r_id = resumed[0]
            resumed_ids = {i for _, i in resumed}

            if r_id == pivot_id:
                verdict = "inclusive_replays_T"
            elif r_id == next_id or (r_tu > pivot_tu and pivot_id not in resumed_ids):
                verdict = "exclusive_resumes_after_T"
            elif r_tu < pivot_tu:
                verdict = "rewinds_before_T"
            elif pivot_id in resumed_ids:
                verdict = "inclusive_replays_T_not_first"
            else:
                verdict = "indeterminate"

            results.append({
                "trial": trial,
                "verdict": verdict,
                "first_resumed_minus_T_us": r_tu - pivot_tu,
                "in_stream_next_minus_T_us": next_tu - pivot_tu,
                "pivot_id_present_in_resume": pivot_id in resumed_ids,
                "resumed_first_equals_pivot": r_id == pivot_id,
                "resumed_first_equals_in_stream_next": r_id == next_id,
            })
            print(f"[cursor] trial {trial}: {verdict} "
                  f"(delta={r_tu - pivot_tu}us)", flush=True)
        except Exception as e:
            results.append({"trial": trial, "error": f"{type(e).__name__}: {e}"})
            print(f"[cursor] trial {trial} error: {e}", flush=True)
        await asyncio.sleep(1.0)

    verdicts = Counter(r.get("verdict") for r in results if r.get("verdict"))
    out = {
        "trials": results,
        "verdict_counts": dict(verdicts),
        "consensus": verdicts.most_common(1)[0][0] if verdicts else None,
        "unanimous": len(verdicts) == 1 and len(results) == args.trials,
    }
    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    (MEASUREMENTS / "cursor_boundary.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# control probes
# ---------------------------------------------------------------------------

async def _count_for(url: str, seconds: float):
    """Count messages and per-collection counts for a fixed duration."""
    counts = Counter()
    total = 0
    first_tu = last_tu = None
    try:
        async with websockets.connect(
            url, max_size=10 * 1024 * 1024, ping_interval=30, ping_timeout=10
        ) as ws:
            deadline = time.monotonic() + seconds
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                total += 1
                tu = msg.get("time_us")
                if isinstance(tu, int):
                    if first_tu is None:
                        first_tu = tu
                    last_tu = tu
                c = msg.get("commit") or {}
                if c.get("collection"):
                    counts[c["collection"]] += 1
                if time.monotonic() > deadline:
                    break
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "total": total}
    return {
        "total": total,
        "seconds": seconds,
        "eps": round(total / seconds, 1),
        "collections": dict(counts.most_common(15)),
        "span_us": (last_tu - first_tu) if (first_tu and last_tu) else None,
    }


async def probe_filtered_vs_unfiltered(url: str, seconds: float):
    """Does omitting wantedCollections yield a superset (the full stream)?

    Run both concurrently over the same wall-clock window and compare the
    post rate. If unfiltered post/sec ~= filtered post/sec, the unfiltered
    connection is not shedding posts to pay for the extra collections.
    """
    unfiltered, filtered = await asyncio.gather(
        _count_for(build_url(url), seconds),
        _count_for(build_url(url, ["app.bsky.feed.post"]), seconds),
    )
    u_posts = (unfiltered.get("collections") or {}).get("app.bsky.feed.post", 0)
    f_posts = (filtered.get("collections") or {}).get("app.bsky.feed.post", 0)
    return {
        "unfiltered": unfiltered,
        "filtered_post_only": filtered,
        "unfiltered_post_count": u_posts,
        "filtered_post_count": f_posts,
        "post_count_ratio_unfiltered_over_filtered": (
            round(u_posts / f_posts, 3) if f_posts else None
        ),
        "unfiltered_distinct_collections": len(unfiltered.get("collections") or {}),
    }


async def probe_retention_horizon(url: str):
    """How far back will Jetstream honour a cursor?

    For each lookback, connect with cursor = now - lookback and record the
    first event's time_us relative to the requested cursor. If the service
    clamps to the present, first_tu - requested will be ~= the lookback.
    """
    out = []
    now_us = int(time.time() * 1_000_000)
    for label, back_s in [
        ("1m", 60), ("10m", 600), ("1h", 3600), ("6h", 21600),
        ("24h", 86400), ("72h", 259200),
    ]:
        requested = now_us - back_s * 1_000_000
        entry = {"lookback": label, "lookback_s": back_s}
        try:
            got = await asyncio.wait_for(
                _collect(build_url(url, ["app.bsky.feed.post"], cursor=requested), 1),
                timeout=30,
            )
            if got:
                first_tu = got[0][0]
                entry["first_event_minus_requested_s"] = round(
                    (first_tu - requested) / 1_000_000, 1
                )
                entry["first_event_behind_now_s"] = round(
                    (now_us - first_tu) / 1_000_000, 1
                )
                entry["served_from_past"] = entry["first_event_behind_now_s"] > back_s * 0.5
            else:
                entry["error"] = "no events"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
        out.append(entry)
        print(f"[retention] {label}: {entry}", flush=True)
    return out


async def probe_slow_consumer(url: str, stall_s: float = 75.0):
    """Connect, then stop reading. Does Jetstream disconnect us, and how?

    The decisive question is not "did we survive" but *what we get back*:
      - first post-stall event lagged by ~stall_s  => backpressure/buffering.
        A slow consumer falls behind visibly. Lag is the loss signal.
      - first post-stall event lagged by ~0        => events were discarded
        somewhere between the relay and us. Silent loss with no lag signal.
    """
    entry = {"stall_s": stall_s}
    try:
        ws = await websockets.connect(
            build_url(url), max_size=10 * 1024 * 1024,
            ping_interval=None, max_queue=4,
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        entry["initial_read_ok"] = True
        entry["initial_lag_s"] = round(
            (time.time() * 1_000_000 - json.loads(raw).get("time_us", 0)) / 1_000_000, 2
        )
        await asyncio.sleep(stall_s)
        entry["closed_during_stall"] = ws.closed
        try:
            raw2 = await asyncio.wait_for(ws.recv(), timeout=10)
            tu2 = json.loads(raw2).get("time_us")
            lag2 = (time.time() * 1_000_000 - tu2) / 1_000_000
            entry["read_after_stall"] = "ok"
            entry["post_stall_lag_s"] = round(lag2, 2)
            if lag2 > stall_s * 0.5:
                entry["interpretation"] = "backpressure_buffered_consumer_falls_behind"
            else:
                entry["interpretation"] = "events_discarded_no_lag_signal"
        except Exception as e:
            entry["read_after_stall"] = f"{type(e).__name__}: {e}"
        entry["close_code"] = ws.close_code
        entry["close_reason"] = str(ws.close_reason) if ws.close_reason else None
        try:
            await ws.close()
        except Exception:
            pass
    except Exception as e:
        entry["error"] = f"{type(e).__name__}: {e}"
    return entry


async def cmd_instances2(args):
    """Sharper cross-instance test.

    Adds a self-control: two independent connections to the SAME instance.
    If the two same-instance connections agree with each other but disagree
    with the other instance, the divergence is server-side and real, not an
    artefact of running several sockets from one host.
    """
    legs = [
        ("jetstream2.us-east#A", INSTANCES["jetstream2.us-east"]),
        ("jetstream2.us-east#B", INSTANCES["jetstream2.us-east"]),
        ("jetstream1.us-east", INSTANCES["jetstream1.us-east"]),
        ("jetstream1.us-west", INSTANCES["jetstream1.us-west"]),
    ]
    results = await asyncio.gather(
        *[_count_for(build_url(u, ["app.bsky.feed.post"]), args.seconds) for _, u in legs],
        return_exceptions=True,
    )
    out = {"seconds": args.seconds, "legs": {}}
    for (name, _), r in zip(legs, results):
        out["legs"][name] = r if not isinstance(r, Exception) else {"error": str(r)}

    def posts(name):
        v = out["legs"].get(name) or {}
        return (v.get("collections") or {}).get("app.bsky.feed.post")

    a, b = posts("jetstream2.us-east#A"), posts("jetstream2.us-east#B")
    j1e, j1w = posts("jetstream1.us-east"), posts("jetstream1.us-west")
    out["same_instance_ratio_A_over_B"] = round(a / b, 3) if a and b else None
    out["cross_instance_ratio_j1east_over_j2east"] = round(j1e / a, 3) if j1e and a else None
    out["cross_instance_ratio_j1west_over_j2east"] = round(j1w / a, 3) if j1w and a else None
    out["_note"] = (
        "Aggregate post counts over one concurrent window. Equal counts are "
        "consistent with, but do not prove, identical event sets; set equality "
        "would require retaining per-event identity, which this project refuses."
    )
    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    (MEASUREMENTS / "instances2.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2, sort_keys=True))


async def cmd_slow(args):
    out = await probe_slow_consumer(args.url, stall_s=args.stall)
    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    (MEASUREMENTS / "slow_consumer.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2, sort_keys=True))


async def probe_instances(seconds: float):
    """Compare two public instances over the same wall-clock window.

    Aggregate-only: per-collection counts. This can show gross divergence.
    It cannot prove set equality without retaining identity, which we refuse.
    """
    names = list(INSTANCES)
    results = await asyncio.gather(
        *[_count_for(build_url(INSTANCES[n], ["app.bsky.feed.post"]), seconds) for n in names],
        return_exceptions=True,
    )
    out = {}
    for n, r in zip(names, results):
        out[n] = r if not isinstance(r, Exception) else {"error": str(r)}
    totals = [v.get("total") for v in out.values() if isinstance(v, dict) and v.get("total")]
    out["_max_pairwise_ratio"] = round(max(totals) / min(totals), 3) if len(totals) > 1 else None
    out["_note"] = (
        "Aggregate counts only. Equal counts are consistent with, but do not "
        "prove, identical event sets. Set comparison would require retaining "
        "per-event identity, which this project refuses."
    )
    return out


async def cmd_cursor_exact(args):
    """Follow-up to `cursor`: if cursor=T is inclusive, is cursor=T+1 exact?

    Exact means: first resumed event == the event that followed T in-stream.
    No replay of T (no overlap) and nothing skipped (no gap).
    """
    results = []
    url_base = build_url(args.url, collections=["app.bsky.feed.post"])
    for trial in range(args.trials):
        try:
            first = await _collect(url_base, args.collect)
            if len(first) < args.pivot + 5:
                results.append({"trial": trial, "error": "insufficient events"})
                continue
            pivot_tu, pivot_id = first[args.pivot]
            next_tu, next_id = first[args.pivot + 1]

            await asyncio.sleep(1.0)
            resumed = await _collect(
                build_url(args.url, ["app.bsky.feed.post"], cursor=pivot_tu + 1), 8
            )
            if not resumed:
                results.append({"trial": trial, "error": "no events on resume"})
                continue
            r_tu, r_id = resumed[0]
            resumed_ids = {i for _, i in resumed}

            replayed_pivot = pivot_id in resumed_ids
            got_next_first = r_id == next_id
            if got_next_first and not replayed_pivot:
                verdict = "exact_no_overlap_no_gap"
            elif replayed_pivot:
                verdict = "still_replays_T"
            elif r_tu > next_tu:
                verdict = "skipped_past_next_gap"
            else:
                verdict = "indeterminate"

            results.append({
                "trial": trial,
                "verdict": verdict,
                "first_resumed_minus_T_us": r_tu - pivot_tu,
                "in_stream_next_minus_T_us": next_tu - pivot_tu,
                "first_resumed_equals_in_stream_next": got_next_first,
                "pivot_replayed": replayed_pivot,
            })
            print(f"[cursor+1] trial {trial}: {verdict}", flush=True)
        except Exception as e:
            results.append({"trial": trial, "error": f"{type(e).__name__}: {e}"})
            print(f"[cursor+1] trial {trial} error: {e}", flush=True)
        await asyncio.sleep(1.0)

    verdicts = Counter(r.get("verdict") for r in results if r.get("verdict"))
    out = {
        "question": "with cursor=T inclusive, does cursor=T+1 resume exactly?",
        "trials": results,
        "verdict_counts": dict(verdicts),
        "consensus": verdicts.most_common(1)[0][0] if verdicts else None,
        "unanimous": len(verdicts) == 1 and len(results) == args.trials,
    }
    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    (MEASUREMENTS / "cursor_exact.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2, sort_keys=True))


async def cmd_control(args):
    url = args.url
    out = {}
    print("[control] filtered vs unfiltered...", flush=True)
    out["filtered_vs_unfiltered"] = await probe_filtered_vs_unfiltered(url, args.seconds)
    print("[control] retention horizon...", flush=True)
    out["retention_horizon"] = await probe_retention_horizon(url)
    print("[control] cross-instance...", flush=True)
    out["instances"] = await probe_instances(args.seconds)
    print("[control] slow consumer...", flush=True)
    out["slow_consumer"] = await probe_slow_consumer(url)
    MEASUREMENTS.mkdir(parents=True, exist_ok=True)
    (MEASUREMENTS / "control_probes.json").write_text(json.dumps(out, indent=2, sort_keys=True))
    print(json.dumps(out, indent=2, sort_keys=True))


def main():
    ap = argparse.ArgumentParser(description="M0 Jetstream verification spike")
    ap.add_argument("--url", default=DEFAULT_URL)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("survey")
    p.add_argument("--seconds", type=float, default=600.0)
    p.set_defaults(fn=cmd_survey)

    p = sub.add_parser("cursor")
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--collect", type=int, default=60)
    p.add_argument("--pivot", type=int, default=40)
    p.set_defaults(fn=cmd_cursor)

    p = sub.add_parser("cursor-exact")
    p.add_argument("--trials", type=int, default=6)
    p.add_argument("--collect", type=int, default=60)
    p.add_argument("--pivot", type=int, default=40)
    p.set_defaults(fn=cmd_cursor_exact)

    p = sub.add_parser("control")
    p.add_argument("--seconds", type=float, default=45.0)
    p.set_defaults(fn=cmd_control)

    p = sub.add_parser("instances2")
    p.add_argument("--seconds", type=float, default=120.0)
    p.set_defaults(fn=cmd_instances2)

    p = sub.add_parser("slow")
    p.add_argument("--stall", type=float, default=75.0)
    p.set_defaults(fn=cmd_slow)

    args = ap.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
