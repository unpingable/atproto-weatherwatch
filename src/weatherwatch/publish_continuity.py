"""Publication continuity: never silently shrink the published history.

The rendered archive is a view of the observation store (see `archive.py`), so
after a store loss a fresh build carries a shorter history. Swapping that in
would quietly replace the public record with less of it. The published site
lives off the collector host, which makes its `history/index.json` the durable
record of what was published before; this module compares a new build against
it before `deploy/publish.sh` swaps anything.

    CONTINUOUS  every published day is still present and the history does not
                start later              -> publish
    SHRUNK      the new history starts later, has fewer days, or drops a
                published day            -> refuse
    UNKNOWN     the published index cannot be read or parsed
                                          -> refuse; never assumed clean

An operator may publish a SHRUNK or UNKNOWN build only by declaring a new
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
UNKNOWN = "UNKNOWN"

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


def evaluate(build_dir: str | Path, published_index: str | Path | None, *,
             declare_new_epoch: bool = False,
             now: str | None = None) -> dict:
    """Compare the build's history extent against the published one.

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
        "notices": [],
    }
    new_index, err = read_index(build / archive.ARCHIVE_DIR / archive.INDEX_NAME)
    if new_index is None:
        result["reasons"].append(f"candidate history index: {err}")
        result["reason"] = "candidate has no readable history index"
        return result
    new_extent = extent(new_index)
    result["candidate"] = new_extent

    published, err = read_index(published_index)
    carried: list = []
    if published is None:
        result["reasons"].append(err)
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
        result["continuity"] = SHRUNK if result["reasons"] else CONTINUOUS

    result["notices"] = carried
    if result["continuity"] == CONTINUOUS:
        result["disposition"] = "PASSED"
        result["reason"] = "published history is preserved"
        return result
    if not declare_new_epoch:
        result["reason"] = (
            "refusing to shrink the published history"
            if result["continuity"] == SHRUNK else
            "continuity with the published history is unknown; refusing")
        return result

    begins = _new_history_begins(build, new_extent)
    declared = now or _iso_now()
    if result["continuity"] == SHRUNK:
        pub = result["published"]
        message = (
            f"History discontinuity declared {declared}. History before "
            f"{begins} was lost: the previously published history "
            f"({pub['first_archived_day']} to {pub['last_archived_day']}, "
            f"{pub['day_count']} archived days) is not part of this one.")
    else:
        message = (
            f"History discontinuity declared {declared}. The previously "
            f"published history could not be read, so whether history "
            f"before {begins} was lost is unknown.")
    result["notices"] = carried + [{
        "schema": NOTICE_SCHEMA,
        "declared_at": declared,
        "continuity": result["continuity"],
        "history_begins": begins,
        "previous_published": result["published"],
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
                    *, declare_new_epoch: bool = False,
                    now: str | None = None) -> dict:
    result = evaluate(build_dir, published_index,
                      declare_new_epoch=declare_new_epoch, now=now)
    if result["disposition"] != "REFUSED":
        apply(build_dir, result["notices"])
    return result
