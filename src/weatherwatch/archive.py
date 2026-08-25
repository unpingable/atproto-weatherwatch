"""The public history contract: a bounded summary and a dated archive beside it.

`summary.json` used to carry every window the collector had ever observed. On
the live estate that was **4.1 MB and 24,609 windows**, growing by about 2,000
windows a day forever, and every visitor's browser fetched all of it to render
a page that reads the last few. The page itself was bounded in an earlier pass;
this is the archive shape that pass deliberately left open.

THE SHAPE
---------
    summary.json            recent windows + every bounded field + a pointer
    history/index.json      what days exist, how big, and their digests
    history/YYYY-MM-DD.json one UTC day of windows, and nothing else

Three properties are load-bearing:

* **Bounded.** `summary.json` carries at most `RECENT_SECONDS` of windows and
  never more than `RECENT_CAP` of them, whatever the bucket width.
* **Nothing disappears.** Every window removed from the summary is written to
  exactly one day file first. The generator refuses to publish a summary whose
  archive it could not write.
* **Nothing is enriched.** A day file carries the *same fields* the summary
  already published for a window and no others. This moves published data; it
  does not sharpen it. A partition boundary is a UTC date, which the `interval`
  block already discloses, so the filenames reveal nothing new either.

WHAT A DAY FILE IS, AND IS NOT
------------------------------
Deterministic, not immutable. The same windows always produce the same bytes —
sorted keys, fixed separators — so a rebuild is a no-op and a rewrite means the
content genuinely changed. But a *closed* day can still change: the collector
accepts late events, and a window can be re-observed by a later run. Claiming
immutability would be a promise this pipeline cannot keep, so instead each day
carries a `digest` and the index republishes it. A consumer that cares can
detect the change; nobody is told a file will never move.

A day file is written only when its digest differs from what is already on
disk, so mtimes do not churn on every five-minute publish.

A VIEW, NOT A VAULT
-------------------
The whole rendered directory is rebuilt into a temp tree and swapped in
atomically, so the archive is regenerated from the observation store on every
publish. Its extent is therefore **the store's extent**, and this module makes
no promise to outlive it: if windows leave the store, their day files stop
being written and the index stops listing them.

That is stated rather than hidden, and it is deliberately not fixed here.
Making the archive durable past the store would be a retention decision about
published artifacts, which is a product question and is owned elsewhere. What
this module guarantees is narrower and checkable: **the recent/archive split
loses nothing** — every window the report would have published is on exactly
one side of it — and any shrinkage of the archive is visible in
`day_count`, `first_archived_day` and the summary's `interval`, never silent.

WHEN AN ARCHIVE FILE IS MISSING OR UNREADABLE
---------------------------------------------
The index lists only day files that were written *and read back successfully*
in this run. A day that failed to serialise is recorded in `problems` rather
than silently omitted — absence with a reason, which is the same posture the
rest of this estate takes toward unobserved time. A client asking for a day the
index does not list should expect 404 and should say so, not infer quiet.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

#: Schema identifiers. `summary.json` previously carried none at all, which
#: left a consumer no way to notice that its shape had changed.
SCHEMA_SUMMARY = "weatherwatch.summary/v2"
SCHEMA_INDEX = "weatherwatch.history-index/v1"
SCHEMA_DAY = "weatherwatch.history-day/v1"

#: How much history stays in `summary.json`.
#:
#: One day, expressed in seconds rather than window counts so it means the same
#: thing whatever the bucket width, with a hard cap so a pathologically narrow
#: bucket cannot reinflate the artifact. At the deployed 60 s width this is
#: 1,440 windows, roughly 160 KB — small enough to fetch on a phone, long
#: enough that the page's own charts and the freshness budget are entirely
#: inside it.
RECENT_SECONDS = 24 * 3600
RECENT_CAP = 2_000

ARCHIVE_DIR = "history"
INDEX_NAME = "index.json"

#: One file per UTC calendar day. Days are the coarsest partition that still
#: lets a client fetch a bounded amount to answer "what happened on the 14th",
#: and they are already the unit the interval block discloses.
DAY_FORMAT = "%Y-%m-%d"


def _day_of(bucket_start: int) -> str:
    return datetime.datetime.fromtimestamp(
        bucket_start, tz=datetime.timezone.utc).strftime(DAY_FORMAT)


def _dumps(document: dict) -> str:
    """One serialiser, so determinism is a property of the module.

    Sorted keys and fixed separators mean identical input yields identical
    bytes, which is what lets `digest` mean something and what makes a rebuild
    a no-op instead of a rewrite.
    """
    return json.dumps(document, indent=2, sort_keys=True,
                      separators=(",", ": "), default=str) + "\n"


def digest_of(text: str) -> str:
    """Content address of one artifact. Truncated: this identifies, it does
    not authenticate, and a full SHA-256 in every index row is noise."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def partition(windows: list, *, recent_seconds: int = RECENT_SECONDS,
              recent_cap: int = RECENT_CAP) -> tuple[list, dict]:
    """Split published windows into (recent, {day: windows}).

    The split is by `bucket_start` against the newest window in the set, not
    against wall clock: the summary describes an observation interval, and a
    report rendered from an archived database should partition the same way it
    would have when that database was live.

    Every window lands in exactly one side. The caller can therefore check that
    nothing was lost by counting, and the tests do.
    """
    if not windows:
        return [], {}

    ordered = sorted(windows, key=lambda w: (w["bucket_start"],
                                             w.get("quality", "")))
    newest = ordered[-1]["bucket_start"]
    floor = newest - recent_seconds

    recent = [w for w in ordered if w["bucket_start"] > floor]
    if len(recent) > recent_cap:
        recent = recent[-recent_cap:]

    # Whatever the cap left behind is archived, so the two sides always
    # reconstruct the whole. Identity is by position, not by value: two runs
    # can each hold a partial piece of the same wall-clock minute, and both
    # rows are real observations that must survive the split.
    cut = len(ordered) - len(recent)
    days: dict = {}
    for window in ordered[:cut]:
        days.setdefault(_day_of(window["bucket_start"]), []).append(window)
    return recent, days


def write_archive(out_dir: str | Path, days: dict, *, generated_at: str,
                  source_endpoint: str = "") -> dict:
    """Write the day files and return the index document.

    Returns the index rather than writing it here so the caller can fold it
    into whatever else it publishes atomically; `write_index` does the write.
    """
    root = Path(out_dir) / ARCHIVE_DIR
    root.mkdir(parents=True, exist_ok=True)

    entries, problems = [], []
    for day in sorted(days):
        windows = sorted(days[day], key=lambda w: (w["bucket_start"],
                                                   w.get("quality", "")))
        document = {
            "schema": SCHEMA_DAY,
            "date": day,
            # Deliberately NOT `generated_at`: a timestamp that moves on every
            # publish would change the bytes of an unchanged day and destroy
            # the only property that makes these files worth caching.
            "source_endpoint": source_endpoint,
            "windows": windows,
            "window_count": len(windows),
            "note": ("One UTC day of observation windows, in the same fields "
                     "and the same precision `summary.json` publishes. "
                     "Unobserved windows appear here marked unobserved; they "
                     "are not absences and they are not zero."),
        }
        text = _dumps(document)
        path = root / f"{day}.json"
        try:
            # Rewrite only on a real change, so a five-minute publish cadence
            # does not churn the mtime of every day ever observed.
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
            # Read back before indexing it: the index promises the file is
            # there and parses, and a promise nobody checks is a rumour.
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append({"date": day, "error": type(exc).__name__})
            continue
        entries.append({
            "date": day,
            "path": f"{ARCHIVE_DIR}/{day}.json",
            "window_count": len(windows),
            "first_bucket_start": windows[0]["bucket_start"],
            "last_bucket_start": windows[-1]["bucket_start"],
            "digest": digest_of(text),
            "bytes": len(text.encode("utf-8")),
        })

    return {
        "schema": SCHEMA_INDEX,
        "generated_at": generated_at,
        "days": entries,
        "day_count": len(entries),
        "window_count": sum(e["window_count"] for e in entries),
        "problems": problems,
        "note": ("Days listed here were written and read back in this run. A "
                 "date absent from this list has no artifact: expect 404 and "
                 "report it as unavailable, never as a quiet period. This "
                 "index is regenerated from the observation store on every "
                 "publish, so its extent is the store's extent; it is a view "
                 "of what is currently observed, not a durable vault."),
    }


def write_index(out_dir: str | Path, index: dict) -> Path:
    path = Path(out_dir) / ARCHIVE_DIR / INDEX_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(index), encoding="utf-8")
    return path


def history_block(index: dict, recent: list, *,
                  recent_seconds: int = RECENT_SECONDS) -> dict:
    """The pointer `summary.json` carries so a consumer can find the rest.

    Named `history` rather than `archive` because that is what it is from the
    reader's side, and it states the bound explicitly: a consumer that does not
    read this block and assumes `windows` is complete is wrong, so the summary
    says so in a field as well as in prose.
    """
    days = index.get("days", [])
    return {
        "windows_are_recent_only": True,
        "recent_seconds": recent_seconds,
        "recent_window_count": len(recent),
        "archived_window_count": index.get("window_count", 0),
        "index": f"{ARCHIVE_DIR}/{INDEX_NAME}",
        "index_schema": SCHEMA_INDEX,
        "day_schema": SCHEMA_DAY,
        "first_archived_day": days[0]["date"] if days else None,
        "last_archived_day": days[-1]["date"] if days else None,
        "day_count": len(days),
        "problems": index.get("problems", []),
        "note": ("`windows` holds only the most recent windows. Older windows "
                 "are published one UTC day per file under `history/`; fetch "
                 "`history/index.json` to see which days exist. The archive "
                 "carries the same fields at the same precision as `windows` "
                 "and adds nothing."),
    }
