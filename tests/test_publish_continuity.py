"""Publication continuity: a build may not silently shrink the published history.

Local directories stand in for the live site; nothing here touches a remote
host or the network.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from weatherwatch import archive, cli, db, publish_continuity, report
from tests.conftest import build_run

REPO = Path(__file__).resolve().parents[1]


def _store(path: Path):
    conn = db.connect(path)
    db.init_db(conn)
    return conn


# --- publication continuity -------------------------------------------------

def _day(n: int) -> str:
    return (dt.date(2026, 9, 1) + dt.timedelta(days=n)).isoformat()


def _index_doc(days: list[str], **extra) -> dict:
    return {"schema": archive.SCHEMA_INDEX, "days": [
        {"date": d, "path": f"history/{d}.json", "window_count": 1440}
        for d in days], "day_count": len(days), **extra}


def _candidate(root: Path, days: list[str]) -> Path:
    (root / "history").mkdir(parents=True)
    archive.write_index(root, _index_doc(days))
    first = int(dt.datetime.fromisoformat(days[0] if days else "2026-10-01")
                .replace(tzinfo=dt.timezone.utc).timestamp())
    (root / "summary.json").write_text(json.dumps(
        {"interval": {"first_bucket_start": first}, "windows": [],
         "store": {"store_id": "store-a"}, "history": {}}))
    (root / "index.html").write_text(
        "<html><head></head><body><main>weather</main></body></html>")
    return root


def _published(root: Path, days: list[str], **extra) -> Path:
    path = root / "live-index.json"
    path.write_text(json.dumps(_index_doc(days, **extra)))
    first = int(dt.datetime.fromisoformat(days[0] if days else "2026-10-01")
                .replace(tzinfo=dt.timezone.utc).timestamp())
    (root / "live-summary.json").write_text(json.dumps(
        {"interval": {"first_bucket_start": first}, "windows": [],
         "store": {"store_id": "store-a"}}))
    return path


def _summary_beside(published: Path) -> Path:
    """The live summary that goes with a live index: `live-summary.json`
    beside a stand-in index, or `summary.json` at the root of a built site."""
    if published.name == "live-index.json":
        return published.parent / "live-summary.json"
    return published.parent.parent / "summary.json"


def _gate(build, published, *flags, summary="beside") -> int:
    args = ["publish-continuity", "--report-dir", str(build)]
    if published is not None:
        args += ["--published-index", str(published)]
    if summary == "beside" and published is not None:
        summary = _summary_beside(Path(published))
    if summary not in (None, "beside"):
        args += ["--published-summary", str(summary)]
    return cli.main(args + list(flags))


def test_publish_guard_passes_a_growing_history(tmp_path):
    build = _candidate(tmp_path / "build", [_day(i) for i in range(4)])
    live = _published(tmp_path, [_day(i) for i in range(3)])
    assert _gate(build, live) == 0
    assert "ww-discontinuity" not in (build / "index.html").read_text()


@pytest.mark.parametrize("new_days", [
    [_day(i) for i in range(5, 8)],     # starts later
    [_day(0), _day(1)],                 # fewer days
    [_day(0), _day(2), _day(3), _day(4)],  # a published day disappears
    [],                                 # nothing archived at all
])
def test_publish_guard_refuses_shrinkage(tmp_path, new_days):
    build = _candidate(tmp_path / "build", new_days)
    live = _published(tmp_path, [_day(i) for i in range(4)])
    before = (build / "index.html").read_text()
    assert _gate(build, live) == 3
    assert (build / "index.html").read_text() == before
    result = publish_continuity.evaluate(build, live, _summary_beside(live))
    assert result["continuity"] == "SHRUNK"
    assert result["disposition"] == "REFUSED"


def test_publish_guard_allows_declared_epoch_with_a_notice(tmp_path):
    build = _candidate(tmp_path / "build", [_day(10)])
    live = _published(tmp_path, [_day(i) for i in range(4)])
    assert _gate(build, live, "--declare-new-epoch") == 0
    page = (build / "index.html").read_text()
    assert "History discontinuity" in page
    assert f"History before {_day(10)}" in page and "was lost" in page
    index = json.loads((build / "history" / "index.json").read_text())
    summary = json.loads((build / "summary.json").read_text())
    for notices in (index["discontinuities"],
                    summary["history"]["discontinuities"]):
        assert notices[-1]["continuity"] == "SHRUNK"
        assert notices[-1]["previous_published"]["first_archived_day"] == _day(0)
    # The next publish compares against the shorter epoch and passes, but it
    # carries the notice forward rather than letting it quietly lapse.
    nxt = _candidate(tmp_path / "next", [_day(10), _day(11)])
    assert _gate(nxt, build / "history" / "index.json") == 0
    assert "was lost" in (nxt / "index.html").read_text()


@pytest.mark.parametrize("published", ["missing", "garbage", "not-an-index"])
def test_publish_guard_refuses_when_published_index_is_unreadable(tmp_path,
                                                                  published):
    build = _candidate(tmp_path / "build", [_day(i) for i in range(9)])
    path = tmp_path / "live-index.json"
    if published == "garbage":
        path.write_text("{not json")
    elif published == "not-an-index":
        path.write_text(json.dumps({"hello": "world"}))
    assert _gate(build, path) == 3
    assert _gate(build, None) == 3
    (tmp_path / "s").mkdir()
    _published(tmp_path / "s", [_day(i) for i in range(4)])
    summary = tmp_path / "s" / "live-summary.json"
    assert publish_continuity.evaluate(build, path, summary)["continuity"] \
        == "UNKNOWN"
    assert _gate(build, path, summary=summary) == 3

    assert _gate(build, path, "--declare-new-epoch", summary=summary) == 0
    page = (build / "index.html").read_text()
    assert "could not be read" in page and "is unknown" in page


def test_publish_script_refuses_shrinkage_and_keeps_the_live_site(tmp_path):
    """End to end through deploy/publish.sh in local mode, with directories
    standing in for the serving host. No network, no remote host."""
    conn = _store(tmp_path / "w.sqlite")
    build_run(conn, "r1", [{"metrics": {"post.create": 5}}
                           for _ in range(2 * 1440)])
    build = tmp_path / "build"
    report.generate_report(conn, build)
    built_days = [d["date"] for d in
                  json.loads((build / "history" / "index.json").read_text())["days"]]
    assert built_days

    target = tmp_path / "www"
    (target / "history").mkdir(parents=True)
    longer = ["2023-11-01", "2023-11-02"] + built_days
    (target / "history" / "index.json").write_text(json.dumps(_index_doc(longer)))
    (target / "summary.json").write_text((build / "summary.json").read_text())
    (target / "index.html").write_text("old live site")

    env = {**os.environ, "WW_MODE": "local", "WW_BUILD": str(build),
           "WW_TARGET": str(target), "WW_URL": "", "WW_PY": sys.executable,
           "WW_CLI": "", "PYTHONPATH": str(REPO / "src")}
    script = str(REPO / "deploy" / "publish.sh")
    refused = subprocess.run(["bash", script, "--skip-generate"], env=env,
                             capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert "REFUSED" in refused.stdout
    assert (target / "index.html").read_text() == "old live site"

    declared = subprocess.run(
        ["bash", script, "--skip-generate", "--declare-new-epoch"], env=env,
        capture_output=True, text=True, timeout=120)
    assert declared.returncode == 0, declared.stdout + declared.stderr
    assert "was lost" in (target / "index.html").read_text()
    live = json.loads((target / "history" / "index.json").read_text())
    assert live["discontinuities"][-1]["previous_published"]["first_archived_day"] == "2023-11-01"


# --- the most recent 24 hours (summary.json) --------------------------------
#
# Sites built the way `report.generate_report` builds them: one window list,
# split by `archive.partition` into the recent windows in `summary.json` and
# day files under `history/`. Minute windows; times in hours from T0.

T0 = int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp())
W = 60


def _windows(start_h: float, end_h: float, *, holes=()) -> list[dict]:
    out = []
    for b in range(T0 + int(start_h * 3600), T0 + int(end_h * 3600), W):
        hole = any(T0 + lo * 3600 <= b < T0 + hi * 3600 for lo, hi in holes)
        out.append({"bucket_start": b, "quality": "unobserved" if hole else "ok",
                    "flags": [], "events_seen": 0 if hole else 5,
                    "observed_duration_us": 0 if hole else W * 1_000_000})
    return out


def _site(root: Path, windows: list[dict], store_id: str = "store-a") -> Path:
    root.mkdir(parents=True)
    recent, days = archive.partition(windows)
    index = archive.write_archive(root, days, generated_at="t")
    archive.write_index(root, index)
    (root / "summary.json").write_text(json.dumps({
        "schema": archive.SCHEMA_SUMMARY,
        "interval": {"first_bucket_start": windows[0]["bucket_start"],
                     "last_bucket_end": windows[-1]["bucket_start"] + W},
        "store": {"store_id": store_id},
        "windows": recent,
        "history": archive.history_block(index, recent)}))
    (root / "index.html").write_text(
        "<html><head></head><body><main>weather</main></body></html>")
    return root


def _check(build, live, *flags):
    code = _gate(build, live / "history" / "index.json", *flags)
    return code, publish_continuity.evaluate(
        build, live / "history" / "index.json", live / "summary.json")


@pytest.mark.parametrize("live_h,new_h", [
    (20, 20.1),   # a few minutes later, nothing archived on either side
    (20, 26),     # the live recent window starts to age into the archive
    (50, 56),
    (50, 80),     # more than a day later: the whole live window is archived
])
def test_recent_window_passes_ordinary_consecutive_publishes(tmp_path,
                                                             live_h, new_h):
    holes = [(3, 4)]  # an old outage stays an outage on both sides
    live = _site(tmp_path / "live", _windows(0, live_h, holes=holes))
    build = _site(tmp_path / "build", _windows(0, new_h, holes=holes))
    code, result = _check(build, live)
    assert code == 0, result["reasons"]
    assert result["continuity"] == "CONTINUOUS"
    assert result["recent"]["candidate_observed_seconds"] \
        >= result["recent"]["published_observed_seconds"]
    assert "ww-discontinuity" not in (build / "index.html").read_text()


def test_recent_window_tolerates_a_small_shortfall(tmp_path):
    live = _site(tmp_path / "live", _windows(0, 20))
    build = _site(tmp_path / "build", _windows(0, 21, holes=[(10, 10.02)]))
    assert _check(build, live)[0] == 0


def test_recent_window_refuses_a_fresh_store_that_starts_later(tmp_path):
    """The live site is younger than a day, so nothing is archived and the
    day-level comparison has nothing to say; the summary does."""
    live = _site(tmp_path / "live", _windows(0, 20))
    build = _site(tmp_path / "build", _windows(19, 21), store_id="store-b")
    before = (build / "index.html").read_text()
    code, result = _check(build, live)
    assert code == 3
    assert result["continuity"] == "SHRUNK"
    assert any("start later" in r for r in result["reasons"])
    assert any("less observed time" in r for r in result["reasons"])
    assert (build / "index.html").read_text() == before


def test_recent_window_refuses_a_loss_inside_the_last_day(tmp_path):
    """Same store, same beginning, every archived day intact; two hours of
    the published last day are gone."""
    live = _site(tmp_path / "live", _windows(0, 50))
    build = _site(tmp_path / "build", _windows(0, 51, holes=[(40, 42)]))
    code, result = _check(build, live)
    assert code == 3 and result["continuity"] == "SHRUNK"
    assert result["reasons"] == [r for r in result["reasons"]
                                 if "less observed time" in r]


def test_store_identity_change_is_refused_without_a_declaration(tmp_path):
    live = _site(tmp_path / "live", _windows(0, 30))
    build = _site(tmp_path / "build", _windows(0, 31), store_id="store-b")
    code, result = _check(build, live)
    assert code == 3
    assert result["continuity"] == "DISCONTINUOUS"
    assert result["store_changed"] == {"published": "store-a",
                                       "candidate": "store-b"}


def test_store_identity_change_publishes_with_a_notice_when_declared(tmp_path):
    live = _site(tmp_path / "live", _windows(0, 30))
    build = _site(tmp_path / "build", _windows(0, 31), store_id="store-b")
    assert _gate(build, live / "history" / "index.json",
                 "--declare-new-epoch") == 0
    page = (build / "index.html").read_text()
    assert "different observation store" in page
    notice = json.loads((build / "summary.json").read_text()) \
        ["history"]["discontinuities"][-1]
    assert notice["continuity"] == "DISCONTINUOUS"
    assert notice["store_changed"]["published"] == "store-a"
    # The next ordinary publish from the new store passes and keeps the notice.
    nxt = _site(tmp_path / "next", _windows(0, 32), store_id="store-b")
    assert _gate(nxt, build / "history" / "index.json") == 0
    assert "different observation store" in (nxt / "index.html").read_text()


def test_declared_epoch_names_the_recent_loss(tmp_path):
    live = _site(tmp_path / "live", _windows(0, 20))
    build = _site(tmp_path / "build", _windows(19, 21), store_id="store-b")
    assert _gate(build, live / "history" / "index.json",
                 "--declare-new-epoch") == 0
    since = dt.datetime.fromtimestamp(T0 + 19 * 3600, dt.timezone.utc) \
        .isoformat().replace("+00:00", "Z")
    page = (build / "index.html").read_text()
    assert f"Observations before {since} in the last 24 hours were lost" in page
    notice = json.loads((build / "history" / "index.json").read_text()) \
        ["discontinuities"][-1]
    assert notice["continuity"] == "SHRUNK"
    assert notice["recent_window"]["candidate_observed_from"] == since


@pytest.mark.parametrize("broken", ["missing", "garbage", "not-a-summary"])
def test_unreadable_live_summary_is_refused(tmp_path, broken):
    live = _site(tmp_path / "live", _windows(0, 30))
    build = _site(tmp_path / "build", _windows(0, 31))
    summary = live / "summary.json"
    if broken == "missing":
        summary.unlink()
    elif broken == "garbage":
        summary.write_text("{not json")
    else:
        summary.write_text(json.dumps({"hello": "world"}))
    code, result = _check(build, live)
    assert code == 3 and result["continuity"] == "UNKNOWN"
    assert _gate(build, live / "history" / "index.json", summary=None) == 3

    assert _gate(build, live / "history" / "index.json",
                 "--declare-new-epoch") == 0
    page = (build / "index.html").read_text()
    assert "last 24 hours were lost is unknown" in page


def _publish(build: Path, target: Path,
             declare: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ, "WW_MODE": "local", "WW_BUILD": str(build),
           "WW_TARGET": str(target), "WW_URL": "", "WW_PY": sys.executable,
           "WW_CLI": "", "PYTHONPATH": str(REPO / "src"),
           "WW_DECLARE_NEW_EPOCH": "1" if declare else "0"}
    return subprocess.run(["bash", str(REPO / "deploy" / "publish.sh"),
                           "--skip-generate"], env=env, capture_output=True,
                          text=True, timeout=120)


def test_publish_script_guards_the_recent_window(tmp_path):
    """End to end through deploy/publish.sh in local mode, from real stores.
    The live site is under a day old, so only the summary can show a loss."""
    conn = _store(tmp_path / "a.sqlite")
    build_run(conn, "r1", [{"metrics": {"post.create": 5}}
                           for _ in range(20 * 60)])
    target = tmp_path / "www"
    first = tmp_path / "build1"
    report.generate_report(conn, first)
    assert not json.loads((first / "history" / "index.json").read_text())["days"]
    published = _publish(first, target)
    # The very first publication has nothing to compare against.
    assert published.returncode != 0 and "REFUSED" in published.stdout
    published = _publish(first, target, declare=True)
    assert published.returncode == 0, published.stdout + published.stderr

    # Ordinary: the same store, ten hours later. Part of the live recent
    # window has aged into the archive; nothing was lost.
    build_run(conn, "r2", [{"metrics": {"post.create": 5}}
                           for _ in range(10 * 60)], start_index=20 * 60,
              started_at="2026-01-01T20:00:00+00:00",
              ended_at="2026-01-02T06:00:00+00:00")
    ordinary = tmp_path / "build2"
    report.generate_report(conn, ordinary)
    result = _publish(ordinary, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED (CONTINUOUS)" in result.stdout
    live_summary = (target / "summary.json").read_text()

    # Catastrophic: the store is lost and a fresh one has two hours.
    fresh = _store(tmp_path / "b.sqlite")
    build_run(fresh, "r9", [{"metrics": {"post.create": 5}}
                            for _ in range(2 * 60)], start_index=28 * 60,
              started_at="2026-01-02T04:00:00+00:00",
              ended_at="2026-01-02T06:00:00+00:00")
    lost = tmp_path / "build3"
    report.generate_report(fresh, lost)
    result = _publish(lost, target)
    assert result.returncode != 0
    assert "REFUSED (SHRUNK)" in result.stdout
    assert "less observed time" in result.stdout
    assert (target / "summary.json").read_text() == live_summary
