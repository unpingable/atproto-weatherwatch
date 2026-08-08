"""M7 — static dashboard generation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from weatherwatch import db, report
from tests.conftest import SYNTH_BASE, SYNTH_ENDPOINT, build_run

FULL = {"post.create": 120, "post.create.reply": 60, "post.create.quote": 10,
        "repost.create": 40, "like.create": 700, "follow.create": 30,
        "block.create": 4, "post.delete": 12, "like.delete": 9,
        "follow.delete": 5, "profile.update": 2, "account.event": 1,
        "identity.event": 1}


@pytest.fixture()
def report_db(conn):
    build_run(conn, "r1", [
        {"metrics": dict(FULL)},
        {"metrics": dict(FULL)},
        {"metrics": dict(FULL), "observed_us": 20_000_000},          # partial
        {"metrics": dict(FULL), "gap_us": 15_000_000},               # gap
        {"metrics": dict(FULL), "coverage_state": "degraded",
         "gate_reasons": "low_eps"},                                 # degraded
        {"metrics": dict(FULL), "resume_seam": 1},                   # seam
        None,                                                        # UNOBSERVED
        {"metrics": dict(FULL)},
        {"metrics": dict(FULL)},
        {"metrics": dict(FULL)},
    ])
    return conn


def read_html(out: Path) -> str:
    return (out / "index.html").read_text()


# --- generation ------------------------------------------------------------

def test_generates_from_synthetic_db(report_db, tmp_path):
    out = tmp_path / "beef"
    stats = report.generate_report(report_db, out)
    assert (out / "index.html").exists()
    assert (out / "summary.json").exists()
    assert stats["windows"] == 9      # 10 slots, one unobserved
    assert stats["html_bytes"] > 2000


def test_generates_from_live_db_if_present(tmp_path):
    live = Path("data/weatherwatch.sqlite")
    if not live.exists():
        pytest.skip("no live database in this checkout")
    conn = db.connect(live)
    stats = report.generate_report(conn, tmp_path / "beef")
    conn.close()
    assert stats["windows"] > 0
    assert (tmp_path / "beef" / "index.html").exists()


def test_refuses_when_no_runs_exist(conn, tmp_path):
    with pytest.raises(ValueError, match="no observation runs"):
        report.generate_report(conn, tmp_path / "beef")


# --- atomic replacement ----------------------------------------------------

def test_output_replacement_is_atomic(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    first = read_html(out)
    (out / "stale-marker.txt").write_text("should not survive")

    report.generate_report(report_db, out)
    assert (out / "index.html").exists()
    assert not (out / "stale-marker.txt").exists(), (
        "regeneration must replace the tree, not merge into it"
    )
    assert len(read_html(out)) > 0
    # No temp or backup directories left behind (the test DB lives here too).
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith(".beef")]
    assert leftovers == [], leftovers


def test_failed_generation_leaves_previous_report_intact(report_db, tmp_path,
                                                         monkeypatch):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    good = read_html(out)

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(report, "_build_html", boom)
    with pytest.raises(RuntimeError):
        report.generate_report(report_db, out)
    assert read_html(out) == good, "a failed run must not damage the old report"


# --- content ---------------------------------------------------------------

def test_exact_observation_source_appears(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert SYNTH_ENDPOINT in html
    summary = json.loads((out / "summary.json").read_text())
    assert summary["source_endpoint"] == SYNTH_ENDPOINT


def test_no_relay_described_as_authoritative(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out).lower()
    assert "no relay is\nauthoritative or complete" in html or \
           "authoritative or complete" in html
    for word in ("ground truth", "canonical source", "complete view"):
        assert word not in html, f"{word!r} implies completeness we cannot claim"


def test_gap_and_degraded_annotations_survive_rendering(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    summary = json.loads((out / "summary.json").read_text())
    qualities = {w["quality"] for w in summary["windows"]}
    assert {"gap", "degraded", "partial", "seam"} <= qualities, qualities
    for q in ("gap", "degraded", "partial", "seam"):
        assert q in html, f"{q} not visible in rendered output"


def test_unobserved_is_structurally_distinct_from_zero(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    summary = json.loads((out / "summary.json").read_text())

    # The unobserved window has no health row at all, so it must not appear
    # as a zero-count observation.
    assert len(summary["windows"]) == 9
    assert all(w["quality"] != "unobserved" for w in summary["windows"])

    # The metric series must report it as unobserved, not as zero.
    assert summary["metrics"]["post.create"]["unobserved_windows"] == 1
    assert summary["metrics"]["post.create"]["observed_windows"] == 9

    # And the rendering must distinguish it: a hatch pattern, plus prose.
    assert 'fill="url(#unobs)"' in html
    assert "not</strong> zero activity" in html or "not zero activity" in html


def test_rates_use_observed_duration_not_nominal(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    summary = json.loads((out / "summary.json").read_text())
    m = summary["metrics"]["post.create"]
    # 9 observed windows: 8 full (60s) + 1 partial (20s) + 1 gapped (45s)
    expected_seconds = 60 * 7 + 20 + 45
    assert m["observed_seconds"] == pytest.approx(expected_seconds)
    assert m["mean_rate_per_s"] == pytest.approx(
        m["total"] / expected_seconds)


def test_beef_index_is_a_disabled_placeholder(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert "GLOBAL BEEF INDEX" in html
    assert "calibration pending" in html
    # No formula, no score, no number attached to it.
    beef = html[html.index("GLOBAL BEEF INDEX"):]
    beef = beef[:beef.index("</div>", beef.index("calibration pending"))]
    assert not re.search(r"\d+\.\d+", beef), "the placeholder must carry no value"


# --- self-containment and privacy ------------------------------------------

def test_no_external_resources_or_links(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert "<script" not in html
    assert "<link" not in html
    assert "<a " not in html and "href=" not in html
    assert "<iframe" not in html
    # The only // occurrences may be the observation endpoint itself.
    for m in re.findall(r"(?:https?:)?//[^\s\"'<>)]+", html):
        assert "relay-a.invalid" in m, f"unexpected external reference: {m}"


def test_report_is_marked_noindex(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    assert 'name="robots"' in read_html(out)
    assert "noindex" in read_html(out)


IDENTITY_PATTERNS = {
    "DID": r"did:[a-z0-9]+:",
    "at-URI": r"at://",
    "CID": r"\bbafy[a-z0-9]{10,}",
    "bsky handle": r"\b[a-z0-9-]+\.bsky\.(social|app)\b",
}


def test_no_identity_in_generated_artifacts(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    for name in ("index.html", "summary.json"):
        text = (out / name).read_text()
        for label, pat in IDENTITY_PATTERNS.items():
            assert not re.search(pat, text, re.I), f"{label} leaked into {name}"


def test_live_report_has_no_identity(tmp_path):
    live = Path("data/weatherwatch.sqlite")
    if not live.exists():
        pytest.skip("no live database in this checkout")
    conn = db.connect(live)
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    conn.close()
    for name in ("index.html", "summary.json"):
        text = (out / name).read_text()
        for label, pat in IDENTITY_PATTERNS.items():
            assert not re.search(pat, text, re.I), f"{label} leaked into {name}"


def test_coverage_denominator_uses_span_not_summed_window_widths(conn, tmp_path):
    """Consecutive runs can each hold part of the same wall-clock minute: a
    clean shutdown commits a partial window and the next run recounts the
    remainder under its own run_id. Summing nominal widths would count that
    minute twice and overstate the interval."""
    build_run(conn, "r1", [
        {"metrics": dict(FULL)},
        {"metrics": dict(FULL), "observed_us": 20_000_000},   # partial tail
    ], start_index=0, started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:02:00+00:00")
    build_run(conn, "r2", [
        {"metrics": dict(FULL), "observed_us": 40_000_000},   # same minute
    ], start_index=1, started_at="2026-01-01T00:02:00+00:00",
        ended_at="2026-01-01T00:03:00+00:00")

    # Realistic event bounds: r1 stopped 20s into minute 1 and committed a
    # partial window; r2 resumed at cursor+1 and covered the remaining 40s.
    # The two runs abut exactly rather than overlapping.
    mid = (SYNTH_BASE + 80) * 1_000_000
    db.end_run(conn, "r1", "2026-01-01T00:02:00+00:00", "duration_reached",
               SYNTH_BASE * 1_000_000, mid)
    db.end_run(conn, "r2", "2026-01-01T00:03:00+00:00", "duration_reached",
               mid, (SYNTH_BASE + 120) * 1_000_000)

    out = tmp_path / "beef"
    report.generate_report(conn, out, run_ids=["r1", "r2"])
    summary = json.loads((out / "summary.json").read_text())
    interval = summary["interval"]

    assert len(summary["windows"]) == 3, "three health rows across two runs"
    assert interval["span_seconds"] == 120, "but only two minutes of wall time"
    assert interval["observed_seconds"] == pytest.approx(120.0)
    assert interval["coverage_ratio"] == pytest.approx(1.0), (
        "the shared minute is fully observed between the two runs"
    )
