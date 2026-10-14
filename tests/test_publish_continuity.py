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
        {"interval": {"first_bucket_start": first}, "history": {}}))
    (root / "index.html").write_text(
        "<html><head></head><body><main>weather</main></body></html>")
    return root


def _published(root: Path, days: list[str], **extra) -> Path:
    path = root / "live-index.json"
    path.write_text(json.dumps(_index_doc(days, **extra)))
    return path


def _gate(build, published, *flags) -> int:
    args = ["publish-continuity", "--report-dir", str(build)]
    if published is not None:
        args += ["--published-index", str(published)]
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
    result = publish_continuity.evaluate(build, live)
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
    assert publish_continuity.evaluate(build, path)["continuity"] == "UNKNOWN"

    assert _gate(build, path, "--declare-new-epoch") == 0
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
