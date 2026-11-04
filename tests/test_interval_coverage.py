"""Duration evidence bounds a union; event timestamps cannot locate it."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from weatherwatch import db, query, report
from tests.conftest import SYNTH_BASE, build_run, build_window


def point(index=0, duration=60_000_000, **kwargs):
    return query.WindowPoint(
        bucket_start=SYNTH_BASE + index * 60, bucket_width=60,
        count=10, events_seen=10, observed_duration_us=duration,
        flags=frozenset(), coverage_state="ok", run_id="test", **kwargs)


@pytest.mark.parametrize("durations,lower,upper", [
    ([60_000_000], 60, 60),
    ([20_000_000], 20, 20),
    ([20_000_000, 40_000_000], 40, 60),  # potentially adjacent
    ([40_000_000, 40_000_000], 40, 60),  # potentially overlapping
    ([60_000_000, 40_000_000], 60, 60),  # full-width dominance
    ([0, 0], 0, 0),
])
def test_duration_only_pieces_do_not_invent_exact_overlap(durations, lower, upper):
    # Summing pieces or treating event bounds as coverage would fail these
    # literal bounds. Locations are deliberately absent from WindowPoint.
    coverage = query.interval_coverage([point(duration=d) for d in durations])
    assert coverage.observed_seconds_lower == lower
    assert coverage.observed_seconds_upper == upper
    assert coverage.observed_seconds == (lower if lower == upper else None)
    assert coverage.coverage_ratio == (lower / 60 if lower == upper else None)
    assert coverage.recorded_observed_seconds == sum(durations) / 1e6


def test_gap_is_not_deducted_twice_and_unobserved_time_remains_in_span():
    gap = replace(point(duration=45_000_000), gap_us=15_000_000,
                  flags=frozenset({query.FLAG_GAP, query.FLAG_SEAM}))
    unobserved = replace(point(1, 0), count=None, events_seen=None,
                         flags=frozenset({query.FLAG_UNOBSERVED}))
    coverage = query.interval_coverage([gap, unobserved, point(2, 30_000_000)])
    assert coverage.span_seconds == 180
    assert coverage.observed_seconds == 75
    assert coverage.coverage_ratio == pytest.approx(75 / 180)


def test_empty_interval_has_no_coverage_fraction():
    coverage = query.interval_coverage([])
    assert coverage.span_seconds == 0
    assert coverage.observed_seconds == 0
    assert coverage.coverage_ratio is None


@pytest.mark.parametrize("duration", [-1, 60_000_001, float("nan"),
                                     float("inf"), float("-inf")])
def test_invalid_duration_evidence_is_rejected_instead_of_clamped(duration):
    with pytest.raises(ValueError, match="observed duration"):
        query.interval_coverage([point(duration=duration)])


@pytest.mark.parametrize("other", [
    replace(point(), bucket_width=120),
    replace(point(), bucket_start=SYNTH_BASE + 30),
    replace(point(), bucket_start=SYNTH_BASE + 90),
])
def test_incompatible_bucket_domains_cannot_add_independent_bounds(other):
    with pytest.raises(ValueError, match="bucket"):
        query.interval_coverage([point(), other])


@pytest.mark.parametrize("width", [30, 60])
def test_health_view_rejects_incompatible_grid_before_densifying(conn, width):
    # Densifying a 60s grid must not silently drop a row at its 30s midpoint,
    # whether it belongs to a second width or to a misaligned 60s run.
    build_run(conn, "r1", [{"metrics": {"post.create": 10}}])
    build_run(conn, "r2", [], started_at="2026-01-01T01:00:00Z")
    conn.execute("UPDATE observation_run SET bucket_width=? WHERE run_id='r2'",
                 (width,))
    build_window(conn, "r2", 0, {"post.create": 5},
                 base=SYNTH_BASE + 30, width=width)
    for rid, lo, hi in [("r1", 0, 20), ("r2", 30, 40)]:
        db.end_run(conn, rid, "2026-01-01T02:00:00Z", "completed",
                   (SYNTH_BASE + lo) * 1_000_000,
                   (SYNTH_BASE + hi) * 1_000_000)
    assert len(query.observation_window_health(conn, ["r1", "r2"], densify=False)) == 2
    with pytest.raises(ValueError, match="bucket"):
        query.observation_window_health(conn, ["r1", "r2"])


def test_report_counts_full_width_evidence_once_without_losing_rows(conn, tmp_path):
    build_run(conn, "r1", [{"metrics": {"post.create": 10}}] * 2)
    build_run(conn, "r2", [{"metrics": {"post.create": 5}}], start_index=1,
              started_at="2026-01-01T01:00:00Z")
    # Event intervals are sequential; historical duration rows can nevertheless
    # cover the same physical minute. Neither event boundary locates coverage.
    seam = (SYNTH_BASE + 80) * 1_000_000
    db.end_run(conn, "r1", "2026-01-01T01:00:00Z", "completed",
               SYNTH_BASE * 1_000_000, seam)
    db.end_run(conn, "r2", "2026-01-01T02:00:00Z", "completed",
               seam + 1, (SYNTH_BASE + 119) * 1_000_000)
    health = query.observation_window_health(conn, ["r1", "r2"])
    assert len(health) == 3
    assert sum(p.observed_seconds for p in health) == 180

    out = tmp_path / "report"
    report.generate_report(conn, out, run_ids=["r1", "r2"])
    summary = json.loads((out / "summary.json").read_text())
    interval = summary["interval"]
    assert interval["span_seconds"] == 120
    assert interval["observed_seconds"] == 120
    assert interval["coverage_ratio"] == 1
    assert interval["recorded_observed_seconds"] == 180
    assert len(summary["windows"]) == 3
    assert summary["metrics"]["post.create"]["total"] == 25
    html = (out / "index.html").read_text()
    assert "100.0% of the interval observed" in html
    assert "150.0%" not in html


def test_report_discloses_partial_piece_uncertainty_replay_and_gaps(conn, tmp_path):
    windows = [{"metrics": {"post.create": 10}}] * 2
    build_run(conn, "r1", windows)
    build_run(conn, "r2", [windows[0], None,
              {"metrics": {"post.create": 10}, "observed_us": 40_000_000,
               "gap_us": 20_000_000, "resume_seam": 1,
               "coverage_state": "degraded", "gate_reasons": "lag_high"}],
              start_index=1, started_at="2026-01-01T01:00:00Z",
              resume_cursor=(SYNTH_BASE + 80) * 1_000_000)
    build_run(conn, "r3", [
              {"metrics": {"post.create": 10}, "observed_us": 40_000_000},
              windows[0]], start_index=3,
              started_at="2026-01-01T02:00:00Z")
    for rid, lo, hi in [("r1", 0, 80), ("r2", 80, 220), ("r3", 220, 299)]:
        db.end_run(conn, rid, "2026-01-01T03:00:00Z", "completed",
                   (SYNTH_BASE + lo) * 1_000_000,
                   (SYNTH_BASE + hi) * 1_000_000)
    run_ids = ["r1", "r2", "r3"]
    before = [tuple(r) for r in conn.execute("SELECT * FROM window_health")]
    before_series = query.series(conn, run_ids, "post.create")
    out = tmp_path / "report"
    report.generate_report(conn, out, run_ids=run_ids)
    summary = json.loads((out / "summary.json").read_text())
    # This actual producer output is the shared Facts/Weather conformance
    # vector. Exclude clock/store identity fields, not coverage uncertainty.
    golden = json.loads((Path(__file__).resolve().parents[1] / "fixtures" /
                         "coverage-bounded-summary.json").read_text())
    stable = ("schema", "interval", "windows", "metrics", "total_events", "runs")
    assert {k: summary[k] for k in stable} == {k: golden[k] for k in stable}
    interval = summary["interval"]
    assert interval["span_seconds"] == 300
    assert interval["recorded_observed_seconds"] == 320
    assert interval["observed_seconds"] is None
    assert interval["coverage_ratio"] is None
    assert interval["observed_seconds_lower"] == 220
    assert interval["observed_seconds_upper"] == 240
    assert interval["coverage_ratio_lower"] == pytest.approx(220 / 300)
    assert interval["coverage_ratio_upper"] == pytest.approx(0.8)
    assert interval["coverage_status"] == "bounded"
    assert interval["multi_piece_buckets"] == 2
    assert interval["uncertain_buckets"] == 1
    assert len(summary["windows"]) == 7  # six recorded rows, one unobserved
    assert summary["total_events"]["total"] == 60
    assert summary["runs"][1]["replayed_from_cursor"] is True
    assert summary["runs"][1]["gap_us"] == 20_000_000
    assert any(w["quality"] == "gap" for w in summary["windows"])
    assert [tuple(r) for r in conn.execute("SELECT * FROM window_health")] == before
    assert query.series(conn, run_ids, "post.create") == before_series
    html = (out / "index.html").read_text()
    assert "73.3–80.0% of the interval observed" in html
    assert "exact overlap is unknown" in html


def test_report_rejects_corrupt_duration_without_replacing_existing_site(conn, tmp_path):
    build_run(conn, "r1", [{"metrics": {"post.create": 10}}])
    out = tmp_path / "report"
    report.generate_report(conn, out)
    previous = (out / "summary.json").read_bytes()
    conn.execute("UPDATE window_health SET observed_duration_us=60000001")
    with pytest.raises(ValueError, match="observed duration"):
        report.generate_report(conn, out)
    assert (out / "summary.json").read_bytes() == previous
