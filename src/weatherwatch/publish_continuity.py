"""Publication continuity: never silently shrink the published history.

The rendered archive is a view of the observation store (see `archive.py`), so
after a store loss a fresh build carries a shorter history. Swapping that in
would quietly replace the public record with less of it. The published site
lives off the collector host, which makes its `history/index.json` the durable
record of what was published before; this module compares a new build against
it before `deploy/publish.sh` swaps anything.

The archive holds whole UTC days; the most recent ~24 hours live only in
`summary.json`. The live summary is therefore compared too: its data interval,
its store identity, and the observed time in its recent windows, which the
candidate must still cover (from its own recent windows or, once they have
aged out, from its archived day files).

    CONTINUOUS    every published day is still present, the history does not
                  start later, the published recent windows are still
                  covered, and the store is the same    -> publish
    SHRUNK        the new history starts later, has fewer days, drops a
                  published day, or covers less of the published recent
                  window                                -> refuse
    DISCONTINUOUS nothing visible was lost, but the build comes from a
                  different observation store            -> refuse
    UNKNOWN       the published index or summary cannot be read or parsed
                                                        -> refuse; never
                                                           assumed clean

An operator may publish a SHRUNK, DISCONTINUOUS or UNKNOWN build only by declaring a new
epoch. The build then carries an explicit discontinuity notice in
`history/index.json`, `summary.json` and the page itself. Notices already on
the published site are carried forward, so later publishes keep saying so.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from pathlib import Path

from . import archive

SCHEMA = "weatherwatch.publish_continuity.v1"
NOTICE_SCHEMA = "weatherwatch.history-discontinuity/v1"

CONTINUOUS = "CONTINUOUS"
SHRUNK = "SHRUNK"
DISCONTINUOUS = "DISCONTINUOUS"
UNKNOWN = "UNKNOWN"

# Observed time the candidate may lack, inside the published recent window,
# before it counts as a loss: whichever is larger. A partial window committed
# at shutdown is recounted, never shortened, so ordinary publishes lose
# nothing; this only absorbs rounding.
RECENT_TOLERANCE_SECONDS = 120
RECENT_TOLERANCE_FRACTION = 0.01

_BANNER_START = "<!--ww-discontinuity-->"
_BANNER_END = "<!--/ww-discontinuity-->"


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0) \
        .isoformat().replace("+00:00", "Z")


def read_index(path: str | Path | None) -> tuple[dict | None, str | None]:
    """Return (index, None) or (None, reason). A malformed index is as
    unknown as a missing one."""
    if path is None:
        return None, "no published index supplied"
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "published index not found"
    except (OSError, ValueError) as exc:
        return None, f"published index unreadable ({type(exc).__name__})"
    if (not isinstance(document, dict)
            or not isinstance(document.get("days"), list)
            or not all(isinstance(d, dict) and isinstance(d.get("date"), str)
                       for d in document["days"])):
        return None, "published index is not a history index"
    return document, None


def extent(index: dict) -> dict:
    days = sorted(d["date"] for d in index.get("days", []))
    return {
        "first_archived_day": days[0] if days else None,
        "last_archived_day": days[-1] if days else None,
        "day_count": len(days),
    }


def _iso(ts) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc) \
        .isoformat().replace("+00:00", "Z")


def read_summary(path: str | Path | None) -> tuple[dict | None, str | None]:
    """Return (summary, None) or (None, reason), like `read_index`."""
    if path is None:
        return None, "no published summary supplied"
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "published summary not found"
    except (OSError, ValueError) as exc:
        return None, f"published summary unreadable ({type(exc).__name__})"
    interval = document.get("interval") if isinstance(document, dict) else None
    windows = document.get("windows") if isinstance(document, dict) else None
    if (not isinstance(interval, dict)
            or not isinstance(windows, list)
            or not all(isinstance(w, dict)
                       and isinstance(w.get("bucket_start"), (int, float))
                       for w in windows)):
        return None, "published summary is not a weatherwatch summary"
    return document, None


def _store_id(summary: dict):
    store = summary.get("store")
    return store.get("store_id") if isinstance(store, dict) else None


def _observed_s(windows) -> float:
    return sum((w.get("observed_duration_us") or 0) for w in windows) / 1e6


def _candidate_windows(build: Path, summary: dict, lo, hi) -> list:
    """Candidate windows with bucket_start in [lo, hi]: its recent windows,
    plus its archived days for windows that have aged out of `summary.json`
    since the live site was published."""
    windows = [w for w in summary.get("windows", [])
               if isinstance(w, dict) and lo <= w.get("bucket_start", -1) <= hi]
    recent_lo = min((w["bucket_start"] for w in summary.get("windows", [])
                     if isinstance(w, dict)
                     and isinstance(w.get("bucket_start"), (int, float))),
                    default=None)
    if recent_lo is not None and recent_lo <= lo:
        return windows
    day = dt.datetime.fromtimestamp(lo, dt.timezone.utc).date()
    last = dt.datetime.fromtimestamp(hi, dt.timezone.utc).date()
    while day <= last:
        path = build / archive.ARCHIVE_DIR / f"{day.isoformat()}.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            archived = document.get("windows", [])
        except (OSError, ValueError, AttributeError):
            archived = []
        windows += [w for w in archived if isinstance(w, dict)
                    and lo <= w.get("bucket_start", -1) <= hi
                    and (recent_lo is None or w["bucket_start"] < recent_lo)]
        day += dt.timedelta(days=1)
    return windows


def compare_recent(build: Path, candidate: dict, published: dict) -> dict:
    """Compare the published summary's interval, store and recent windows
    with the candidate's. Returns {"reasons", "loss", "store_changed", ...};
    `loss` reasons mean SHRUNK, a store change alone means DISCONTINUOUS."""
    out: dict = {"loss": [], "store_changed": None, "recent": None,
                 "recent_lost": False, "start_later": False}
    pub_first = (published.get("interval") or {}).get("first_bucket_start")
    new_first = (candidate.get("interval") or {}).get("first_bucket_start")
    if isinstance(pub_first, (int, float)) and (
            not isinstance(new_first, (int, float)) or new_first > pub_first):
        out["start_later"] = True
        out["loss"].append(
            "data interval would start later ("
            f"{_iso(new_first) if isinstance(new_first, (int, float)) else 'no data'}"
            f" instead of {_iso(pub_first)})")

    pub_windows = published.get("windows", [])
    if pub_windows:
        lo = min(w["bucket_start"] for w in pub_windows)
        hi = max(w["bucket_start"] for w in pub_windows)
        live_s = _observed_s(pub_windows)
        cand = _candidate_windows(build, candidate, lo, hi)
        cand_s = _observed_s(cand)
        allowed = max(RECENT_TOLERANCE_SECONDS,
                      RECENT_TOLERANCE_FRACTION * live_s)
        observed_from = min((w["bucket_start"] for w in cand
                             if (w.get("observed_duration_us") or 0) > 0),
                            default=None)
        out["recent"] = {
            "published_from": _iso(lo), "published_to": _iso(hi),
            "published_observed_seconds": round(live_s, 3),
            "candidate_observed_seconds": round(cand_s, 3),
            "candidate_observed_from": (_iso(observed_from)
                                        if observed_from is not None else None),
        }
        out["recent_lost"] = live_s - cand_s > allowed
        if out["recent_lost"]:
            out["loss"].append(
                f"recent window ({_iso(lo)} to {_iso(hi)}) would cover "
                f"{live_s - cand_s:.0f} s less observed time "
                f"({cand_s:.0f} s instead of {live_s:.0f} s)")

    pub_store, new_store = _store_id(published), _store_id(candidate)
    if pub_store is not None and new_store != pub_store:
        out["store_changed"] = {"published": pub_store, "candidate": new_store}
    return out


def _recent_message(recent: dict | None) -> str:
    """The notice's sentence about the last 24 hours. `recent` is the
    `compare_recent` result, or None when the live summary was unreadable."""
    if recent is None:
        return (" Whether observations in the last 24 hours were lost is "
                "unknown.")
    window = recent.get("recent")
    if not window or not recent.get("recent_lost"):
        return ""
    since = window["candidate_observed_from"]
    span = f"{window['published_from']} to {window['published_to']}"
    if since is None:
        return (f" All observations published for the last 24 hours ({span}) "
                "were lost.")
    return (f" Observations before {since} in the last 24 hours were lost "
            f"(published window {span}).")


def _new_history_begins(build: Path, new_extent: dict) -> str:
    try:
        summary = json.loads((build / "summary.json").read_text("utf-8"))
        first = (summary.get("interval") or {}).get("first_bucket_start")
    except (OSError, ValueError):
        first = None
    if isinstance(first, (int, float)):
        return dt.datetime.fromtimestamp(first, dt.timezone.utc) \
            .isoformat().replace("+00:00", "Z")
    return new_extent["first_archived_day"] or "unknown"


def evaluate(build_dir: str | Path, published_index: str | Path | None,
             published_summary: str | Path | None = None, *,
             declare_new_epoch: bool = False,
             now: str | None = None) -> dict:
    """Compare the build's history extent, data interval, recent windows and
    store identity against the published ones.

    Pure apart from reading files: it decides, `apply` writes.
    """
    build = Path(build_dir)
    result: dict = {
        "schema": SCHEMA,
        "report_dir": str(build),
        "declare_new_epoch": bool(declare_new_epoch),
        "continuity": UNKNOWN,
        "disposition": "REFUSED",
        "reasons": [],
        "published": None,
        "candidate": None,
        "recent": None,
        "store_changed": None,
        "notices": [],
    }
    new_index, err = read_index(build / archive.ARCHIVE_DIR / archive.INDEX_NAME)
    if new_index is None:
        result["reasons"].append(f"candidate history index: {err}")
        result["reason"] = "candidate has no readable history index"
        return result
    new_summary, err = read_summary(build / "summary.json")
    if new_summary is None:
        result["reasons"].append(f"candidate summary: {err}")
        result["reason"] = "candidate has no readable summary"
        return result
    new_extent = extent(new_index)
    result["candidate"] = new_extent

    published, err = read_index(published_index)
    carried: list = []
    unknown = False
    lost: list = []
    history_lost = False
    if published is None:
        result["reasons"].append(err)
        unknown = True
    else:
        pub_extent = extent(published)
        result["published"] = pub_extent
        carried = [n for n in published.get("discontinuities", [])
                   if isinstance(n, dict)]
        pub_days = {d["date"] for d in published["days"]}
        new_days = {d["date"] for d in new_index.get("days", [])}
        missing = sorted(pub_days - new_days)
        pub_first, new_first = (pub_extent["first_archived_day"],
                                new_extent["first_archived_day"])
        if pub_first is not None and (new_first is None or new_first > pub_first):
            result["reasons"].append(
                f"history would start later ({new_first or 'nothing archived'} "
                f"instead of {pub_first})")
        if new_extent["day_count"] < pub_extent["day_count"]:
            result["reasons"].append(
                f"history would have fewer days ({new_extent['day_count']} "
                f"instead of {pub_extent['day_count']})")
        if missing:
            result["reasons"].append(
                f"{len(missing)} published day(s) would disappear")
        result["missing_days"] = missing
        lost += list(result["reasons"])
        history_lost = bool(lost)

    live_summary, err = read_summary(published_summary)
    recent = None
    if live_summary is None:
        result["reasons"].append(err)
        unknown = True
    else:
        recent = compare_recent(build, new_summary, live_summary)
        result["recent"] = recent["recent"]
        result["store_changed"] = recent["store_changed"]
        result["reasons"] += recent["loss"]
        lost += recent["loss"]
        history_lost = history_lost or recent["start_later"]
        if recent["store_changed"]:
            change = recent["store_changed"]
            result["reasons"].append(
                f"observation store changed ({change['published']} -> "
                f"{change['candidate'] or 'no identity'})")

    if unknown:
        result["continuity"] = UNKNOWN
    elif lost:
        result["continuity"] = SHRUNK
    elif result["store_changed"]:
        result["continuity"] = DISCONTINUOUS
    else:
        result["continuity"] = CONTINUOUS

    result["notices"] = carried
    if result["continuity"] == CONTINUOUS:
        result["disposition"] = "PASSED"
        result["reason"] = "published history is preserved"
        return result
    if not declare_new_epoch:
        result["reason"] = {
            SHRUNK: "refusing to shrink the published history",
            DISCONTINUOUS: "refusing to replace the published history with "
                           "one from a different observation store",
        }.get(result["continuity"],
              "continuity with the published history is unknown; refusing")
        return result

    begins = _new_history_begins(build, new_extent)
    declared = now or _iso_now()
    pub = result["published"]
    if published is None:
        message = (
            f"History discontinuity declared {declared}. The previously "
            f"published history could not be read, so whether history "
            f"before {begins} was lost is unknown.")
    elif history_lost:
        message = (
            f"History discontinuity declared {declared}. History before "
            f"{begins} was lost: the previously published history "
            f"({pub['first_archived_day']} to {pub['last_archived_day']}, "
            f"{pub['day_count']} archived days) is not part of this one.")
    else:
        message = (
            f"History discontinuity declared {declared}. The previously "
            f"published archive ({pub['first_archived_day']} to "
            f"{pub['last_archived_day']}) is preserved.")
    message += _recent_message(recent)
    if result["store_changed"]:
        message += (" This history comes from a different observation store "
                    "than the one previously published, and does not "
                    "continue it.")
    result["notices"] = carried + [{
        "schema": NOTICE_SCHEMA,
        "declared_at": declared,
        "continuity": result["continuity"],
        "history_begins": begins,
        "previous_published": result["published"],
        "recent_window": result["recent"],
        "store_changed": result["store_changed"],
        "reasons": list(result["reasons"]),
        "message": message,
    }]
    result["disposition"] = "NEW_EPOCH"
    result["reason"] = "operator declared a new history epoch"
    return result


def _banner(notices: list) -> str:
    items = "".join(f"<p style=\"margin:.25em 0\">{html.escape(n.get('message', ''))}</p>"
                    for n in notices)
    return (f"{_BANNER_START}<div class=\"ww-discontinuity\" role=\"alert\" "
            "style=\"border:3px solid #b3261e;background:#fdecea;color:#5f1410;"
            "padding:.75em 1em;margin:0;font:600 1rem/1.4 system-ui,sans-serif\">"
            "<strong>History discontinuity.</strong>"
            f"{items}</div>{_BANNER_END}")


def apply(build_dir: str | Path, notices: list) -> None:
    """Write the notices into the build. Idempotent; no-op without notices."""
    if not notices:
        return
    build = Path(build_dir)
    index_path = build / archive.ARCHIVE_DIR / archive.INDEX_NAME
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["discontinuities"] = notices
    archive.write_index(build, index)

    summary_path = build / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.setdefault("history", {})["discontinuities"] = notices
    summary_path.write_text(json.dumps(summary, indent=2, default=str),
                            encoding="utf-8")

    page = build / "index.html"
    text = page.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(_BANNER_START) + ".*?" + re.escape(_BANNER_END),
                         re.S)
    banner = _banner(notices)
    if pattern.search(text):
        text = pattern.sub(lambda _m: banner, text, count=1)
    else:
        text, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + banner,
                          text, count=1)
        if not n:
            text = banner + text
    page.write_text(text, encoding="utf-8")


def check_and_apply(build_dir: str | Path, published_index: str | Path | None,
                    published_summary: str | Path | None = None,
                    *, declare_new_epoch: bool = False,
                    now: str | None = None) -> dict:
    result = evaluate(build_dir, published_index, published_summary,
                      declare_new_epoch=declare_new_epoch, now=now)
    if result["disposition"] != "REFUSED":
        apply(build_dir, result["notices"])
    return result
