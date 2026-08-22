"""The seismogram: renders, and carries nothing it should not."""

from __future__ import annotations

import json

from weatherwatch.social import episodes, report
from weatherwatch.social.sensors import aggregate

from .conftest import build_run, steady_then_burst
from .test_boundaries import IDENTITY_MARKERS

RUN = "run-report"


def _envs(conn):
    windows = []
    for i in range(20):
        windows.append({"metrics": {"block.create": 10 + (i % 3) - 1,
                                    "like.create": 10 + (i % 3) - 1}})
    for _ in range(4):
        windows.append({"metrics": {"block.create": 90, "like.create": 90}})
    windows += [{"metrics": {"block.create": 10, "like.create": 10}}] * 6
    build_run(conn, RUN, windows)
    sealed = episodes.run_aggregate(
        conn, [RUN], ["block.create", "like.create"], None, None,
        aggregate.AggregateConfig(detect_lulls=False))
    return [e for e, _ in sealed]


def test_report_renders_and_names_no_one(conn, tmp_path):
    envs = _envs(conn)
    pairs = episodes.co_occurrence(envs)
    path = report.generate(envs, pairs, {"generated_at": "t"}, tmp_path)
    html = path.read_text()

    assert "<svg" in html and "seismogram" in html
    for e in envs:
        assert e.type in html
    for marker in IDENTITY_MARKERS:
        if marker == "@":
            continue          # appears in @media queries
        assert marker not in html, f"report leaked {marker!r}"


def test_report_has_no_lookup_or_ranking_surface(conn, tmp_path):
    envs = _envs(conn)
    html = report.generate(envs, [], {}, tmp_path).read_text()
    # Structural markers only: the page's own prose says the word "ranking"
    # in the course of disclaiming it, and a substring check on prose would
    # forbid the disclaimer along with the feature.
    for shape in ("<input", "<form", "<select", "<button",
                  "leaderboard", "top blockers", "worst offenders",
                  "onclick=", "fetch(", "xmlhttprequest"):
        assert shape not in html.lower(), f"report grew a {shape!r} surface"
    assert 'name="robots" content="noindex,nofollow"' in html


def test_json_sidecar_round_trips_the_envelopes(conn, tmp_path):
    envs = _envs(conn)
    report.generate(envs, [], {"k": "v"}, tmp_path)
    data = json.loads((tmp_path / "episodes.json").read_text())
    assert len(data["episodes"]) == len(envs)
    assert {e["subject"]["type"] for e in data["episodes"]} == {"episode"}


def test_empty_range_renders_without_crashing(tmp_path):
    html = report.generate([], [], {"k": "v"}, tmp_path).read_text()
    assert "No episodes in range." in html
