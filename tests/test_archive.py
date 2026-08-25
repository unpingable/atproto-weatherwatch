"""The public history contract: bounded summary, dated archive, nothing lost.

`summary.json` reached 4.1 MB and 24,609 windows on the live estate and grew
by about 2,000 windows a day forever. These tests pin the shape that replaced
it, and they are written to fail in the two directions that matter: the
artifact growing again, and a window falling down the gap between the recent
tail and the archive.

They also pin what the split must NOT do. Moving published data into dated
files is only safe while it stays the *same* data — same fields, same
precision, same coarseness. A test that only checked sizes would let the
archive quietly become richer than the summary it came from.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from weatherwatch import archive, db, report
from tests.conftest import SYNTH_BASE, build_run

FULL = {"post.create": 120, "post.create.reply": 60, "like.create": 700,
        "follow.create": 30, "block.create": 4, "post.delete": 12}

#: The exact fields a published window has carried since v1. The archive may
#: not add to this set, and neither may the recent tail.
WINDOW_FIELDS = {"bucket_start", "quality", "flags", "events_seen",
                 "observed_duration_us"}

IDENTITY_RE = re.compile(
    r"did:(plc|web|key):|at://|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)|\ba:[0-9a-f]{12}\b")


def _render(conn, tmp_path, windows: int, name: str = "site") -> Path:
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(windows)])
    out = tmp_path / name
    report.generate_report(conn, out)
    return out


def _summary(out: Path) -> dict:
    return json.loads((out / "summary.json").read_text())


def _index(out: Path) -> dict:
    return json.loads((out / "history" / "index.json").read_text())


def _archived_windows(out: Path) -> list:
    got = []
    for entry in _index(out)["days"]:
        doc = json.loads((out / entry["path"]).read_text())
        got.extend(doc["windows"])
    return got


# --- 1. the summary is bounded ---------------------------------------------

def test_summary_stays_bounded_across_a_long_history(conn, tmp_path):
    """The defect, as a test: 30 days of minute windows must not mean 30 days
    of JSON in the artifact every visitor downloads."""
    out = _render(conn, tmp_path, 30 * 1440)
    summary = _summary(out)
    assert len(summary["windows"]) <= archive.RECENT_CAP
    size = (out / "summary.json").stat().st_size
    assert size < 400_000, f"summary.json is {size:,} bytes"


def test_the_summary_does_not_grow_with_the_archive(conn, tmp_path):
    small = _render(conn, tmp_path, 2 * 1440, name="small")
    big_conn = db.connect(tmp_path / "big.sqlite")
    db.init_db(big_conn)
    big = _render(big_conn, tmp_path, 20 * 1440, name="big")

    a = (small / "summary.json").stat().st_size
    b = (big / "summary.json").stat().st_size
    assert b < a * 1.3, f"summary grew {a:,} -> {b:,} on 10x the history"
    assert _index(big)["day_count"] > _index(small)["day_count"], (
        "the archive should be the thing that grew")


def test_a_short_history_needs_no_archive_at_all(conn, tmp_path):
    out = _render(conn, tmp_path, 60)
    assert _summary(out)["windows"], "recent windows should still be present"
    assert _index(out)["days"] == []
    assert _summary(out)["history"]["archived_window_count"] == 0


# --- 2. nothing is lost across the boundary --------------------------------

def test_every_window_lands_on_exactly_one_side(conn, tmp_path):
    out = _render(conn, tmp_path, 5 * 1440)
    summary = _summary(out)
    recent = summary["windows"]
    archived = _archived_windows(out)

    assert len(recent) + len(archived) == 5 * 1440
    starts = [w["bucket_start"] for w in recent + archived]
    assert len(starts) == len(set(starts)), "a window appears twice"
    assert summary["history"]["archived_window_count"] == len(archived)
    assert summary["history"]["recent_window_count"] == len(recent)


def test_the_boundary_has_no_gap(conn, tmp_path):
    """The newest archived window must be adjacent to the oldest recent one."""
    out = _render(conn, tmp_path, 4 * 1440)
    recent = _summary(out)["windows"]
    archived = _archived_windows(out)
    assert archived and recent
    newest_archived = max(w["bucket_start"] for w in archived)
    oldest_recent = min(w["bucket_start"] for w in recent)
    assert oldest_recent > newest_archived
    assert oldest_recent - newest_archived == 60, "a window fell down the gap"


def test_the_split_is_lossless_at_the_unit_level():
    windows = [{"bucket_start": 1_700_000_000 + i * 60, "quality": "clean",
                "flags": [], "events_seen": i, "observed_duration_us": 60_000_000}
               for i in range(5000)]
    recent, days = archive.partition(windows)
    total = len(recent) + sum(len(v) for v in days.values())
    assert total == len(windows)
    seen = [w["bucket_start"] for w in recent]
    for day in days.values():
        seen.extend(w["bucket_start"] for w in day)
    assert sorted(seen) == sorted(w["bucket_start"] for w in windows)


def test_two_runs_sharing_a_minute_both_survive_the_split():
    """Consecutive runs can each hold a partial piece of the same minute; the
    split is by position, not by value, so neither is deduplicated away."""
    base = 1_700_000_000
    windows = [
        {"bucket_start": base, "quality": "partial", "flags": ["partial"],
         "events_seen": 5, "observed_duration_us": 20_000_000},
        {"bucket_start": base, "quality": "clean", "flags": [],
         "events_seen": 9, "observed_duration_us": 40_000_000},
    ]
    recent, days = archive.partition(windows, recent_seconds=0)
    assert len(recent) + sum(len(v) for v in days.values()) == 2


# --- 3. the archive is retrievable and says what it holds -------------------

def test_history_is_retrievable_through_the_index(conn, tmp_path):
    out = _render(conn, tmp_path, 6 * 1440)
    index = _index(out)
    assert index["days"], "nothing was archived"
    for entry in index["days"]:
        path = out / entry["path"]
        assert path.is_file(), f"index lists a missing file: {entry['path']}"
        doc = json.loads(path.read_text())
        assert doc["date"] == entry["date"]
        assert doc["window_count"] == entry["window_count"] == len(doc["windows"])
        assert archive.digest_of(path.read_text()) == entry["digest"]


def test_the_summary_points_at_the_index(conn, tmp_path):
    out = _render(conn, tmp_path, 4 * 1440)
    history = _summary(out)["history"]
    assert history["windows_are_recent_only"] is True
    assert (out / history["index"]).is_file()
    assert history["day_count"] == _index(out)["day_count"]
    assert history["first_archived_day"] < history["last_archived_day"]


def test_every_archived_day_is_a_utc_calendar_day(conn, tmp_path):
    out = _render(conn, tmp_path, 4 * 1440)
    import datetime
    for entry in _index(out)["days"]:
        doc = json.loads((out / entry["path"]).read_text())
        for w in doc["windows"]:
            day = datetime.datetime.fromtimestamp(
                w["bucket_start"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
            assert day == entry["date"], "a window is filed under the wrong day"


# --- 4. determinism ---------------------------------------------------------

def test_archive_generation_is_deterministic(conn, tmp_path):
    import datetime
    build_run(conn, "r1", [{"metrics": dict(FULL)} for _ in range(3 * 1440)])
    now = datetime.datetime.fromtimestamp(
        SYNTH_BASE + 3 * 1440 * 60, tz=datetime.timezone.utc)
    a, b = tmp_path / "a", tmp_path / "b"
    report.generate_report(conn, a, now=now)
    report.generate_report(conn, b, now=now)
    for entry in _index(a)["days"]:
        assert (a / entry["path"]).read_text() == (b / entry["path"]).read_text()
    assert (a / "history" / "index.json").read_text() == \
           (b / "history" / "index.json").read_text()


def test_a_day_file_carries_no_generation_timestamp(conn, tmp_path):
    """A moving timestamp would change the bytes of an unchanged day and
    destroy the only property that makes these files cacheable."""
    out = _render(conn, tmp_path, 3 * 1440)
    doc = json.loads((out / _index(out)["days"][0]["path"]).read_text())
    assert "generated_at" not in doc


def test_rewriting_an_unchanged_day_is_a_no_op(tmp_path):
    windows = [{"bucket_start": 1_700_000_000 + i * 60, "quality": "clean",
                "flags": [], "events_seen": i,
                "observed_duration_us": 60_000_000} for i in range(120)]
    _recent, days = archive.partition(windows, recent_seconds=0)
    archive.write_archive(tmp_path, days, generated_at="t1")
    day = sorted(days)[0]
    path = tmp_path / archive.ARCHIVE_DIR / f"{day}.json"
    first = path.stat().st_mtime_ns
    archive.write_archive(tmp_path, days, generated_at="t2-later")
    assert path.stat().st_mtime_ns == first, "unchanged day was rewritten"


# --- 5. precision and privacy are not increased ----------------------------

def test_the_archive_adds_no_field_the_summary_did_not_publish(conn, tmp_path):
    out = _render(conn, tmp_path, 4 * 1440)
    for w in _archived_windows(out):
        assert set(w) == WINDOW_FIELDS, f"archive window has {set(w)}"
    for w in _summary(out)["windows"]:
        assert set(w) == WINDOW_FIELDS


def test_archive_timestamps_are_no_finer_than_the_summarys(conn, tmp_path):
    """`bucket_start` is a whole-second bucket boundary in both, and the
    archive must not introduce sub-second or per-event timing."""
    out = _render(conn, tmp_path, 4 * 1440)
    width = 60
    for w in _archived_windows(out):
        assert isinstance(w["bucket_start"], int)
        assert w["bucket_start"] % width == 0


def test_no_identity_reaches_any_archive_artifact(conn, tmp_path):
    out = _render(conn, tmp_path, 4 * 1440)
    for path in sorted((out / "history").rglob("*.json")):
        assert not IDENTITY_RE.search(path.read_text()), path.name


def test_archive_filenames_disclose_only_a_date(conn, tmp_path):
    """A filename is metadata a caching proxy and a log will keep, so it must
    not carry a run id, an endpoint, or anything the interval did not."""
    out = _render(conn, tmp_path, 4 * 1440)
    for path in (out / "history").glob("*.json"):
        assert path.name == "index.json" or \
            re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", path.name), path.name


def test_the_archive_does_not_publish_per_metric_series(conn, tmp_path):
    """Windows carry a total event count, not a breakdown. Publishing
    per-metric counts per window would be new precision, not a new location."""
    out = _render(conn, tmp_path, 3 * 1440)
    doc = json.loads((out / _index(out)["days"][0]["path"]).read_text())
    assert "metrics" not in doc
    for w in doc["windows"]:
        assert "metrics" not in w


# --- 6. degradation --------------------------------------------------------

def test_a_day_that_cannot_be_written_is_reported_not_dropped(tmp_path,
                                                              monkeypatch):
    windows = [{"bucket_start": 1_700_000_000 + i * 60, "quality": "clean",
                "flags": [], "events_seen": i,
                "observed_duration_us": 60_000_000} for i in range(2880)]
    _recent, days = archive.partition(windows, recent_seconds=0)

    real = Path.write_text
    def fail_one(self, *a, **kw):
        if self.name.endswith("2023-11-14.json"):
            raise OSError("disk full")
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "write_text", fail_one)

    index = archive.write_archive(tmp_path, days, generated_at="t")
    assert index["problems"], "a failed day vanished silently"
    listed = {e["date"] for e in index["days"]}
    assert "2023-11-14" not in listed, "a day that failed was still indexed"


def test_the_index_never_lists_a_file_it_did_not_read_back(conn, tmp_path):
    out = _render(conn, tmp_path, 4 * 1440)
    for entry in _index(out)["days"]:
        assert (out / entry["path"]).is_file()


def test_an_empty_report_produces_an_empty_but_valid_index(tmp_path):
    index = archive.write_archive(tmp_path, {}, generated_at="t")
    assert index["days"] == [] and index["problems"] == []
    assert index["schema"] == archive.SCHEMA_INDEX


# --- 7. compatibility -------------------------------------------------------

def test_existing_summary_fields_are_unchanged(conn, tmp_path):
    """A consumer reading anything other than the full window list must not
    notice this change."""
    out = _render(conn, tmp_path, 4 * 1440)
    summary = _summary(out)
    for key in ("interval", "generated_at", "freshness", "conditions",
                "collector_version", "claim", "measures", "does_not_measure",
                "source_endpoint", "runs", "metrics", "total_events", "notes"):
        assert key in summary, f"summary.json lost {key!r}"
    assert summary["interval"]["first_bucket_start"] is not None
    # the interval still describes the WHOLE observed span, not the tail
    assert summary["interval"]["first_bucket_start"] == min(
        w["bucket_start"] for w in _archived_windows(out))


def test_the_summary_declares_its_schema(conn, tmp_path):
    out = _render(conn, tmp_path, 100)
    assert _summary(out)["schema"] == archive.SCHEMA_SUMMARY


def test_windows_keeps_its_name_and_element_shape(conn, tmp_path):
    """Shallow consumers that read `windows` for recent state keep working;
    only the assumption that it is complete is broken, and `history` says so."""
    out = _render(conn, tmp_path, 4 * 1440)
    summary = _summary(out)
    assert isinstance(summary["windows"], list) and summary["windows"]
    assert set(summary["windows"][0]) == WINDOW_FIELDS
    assert summary["history"]["windows_are_recent_only"] is True
