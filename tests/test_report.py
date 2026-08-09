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


# --- presentation hygiene (M7 cleanup campaign) ----------------------------

def test_charts_have_no_fixed_pixel_width(report_db, tmp_path):
    """A fixed `width="300"` on an SVG gives it an intrinsic width, which a
    grid child with the default min-width:auto then refuses to shrink below —
    so the chart paints outside its card. No chart may carry pixel width."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert not re.search(r'<svg[^>]*\swidth="\d', html), (
        "chart markup carries a fixed pixel width"
    )
    assert not re.search(r'<svg[^>]*\sheight="\d', html)


def test_charts_are_responsive_and_scale_by_viewbox(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    svgs = re.findall(r"<svg[^>]*>", html)
    assert svgs
    for tag in svgs:
        assert "viewBox=" in tag, f"no viewBox: {tag}"
        assert re.search(r'class="(spark|strip)"', tag), f"no sizing class: {tag}"
    # and the classes must actually be sized responsively in the stylesheet
    assert re.search(r"\.spark\s*\{[^}]*width:100%", html)
    assert re.search(r"\.strip\s*\{[^}]*width:100%", html)
    assert re.search(r"svg\s*\{[^}]*max-width:100%", html)


def test_grid_children_can_shrink(report_db, tmp_path):
    """min-width:0 on grid children is the actual fix for the overflow; the
    minmax() floors must also collapse rather than forcing page width."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert re.search(r"\.grid\s*>\s*\*[^{]*\{[^}]*min-width:0", html)
    for floor in ("330px", "232px"):
        assert f"minmax(min({floor},100%),1fr)" in html, (
            f"minmax floor {floor} is hard and will overflow narrow viewports"
        )


def test_long_operational_strings_are_structurally_contained(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert "overflow-wrap:anywhere" in html
    # break-all splits ordinary numbers mid-digit ("5053 9"); it must not return
    assert "word-break:break-all" not in html


def test_dense_tables_have_local_scrollers(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    tables = re.findall(r"<table", html)
    wrappers = re.findall(r'class="panel scroll[^"]*"', html)
    assert len(wrappers) >= len(tables), "every table needs a local scroller"
    assert re.search(r"\.scroll\s*\{[^}]*overflow-x:auto", html)
    # a min-width is what makes the scroller engage instead of the table
    # squeezing itself into illegibility
    assert re.search(r"\.scroll table\s*\{[^}]*min-width", html)


def test_saturated_lag_is_not_shown_as_an_exact_value(conn, tmp_path):
    """health.record_event_time clamps every sample to LAG_CLAMP_MAX_S, so a
    window stuck an hour behind records exactly 600.000s — the same number as
    one ten minutes behind. Printing that as exact is a lie of precision."""
    from weatherwatch import health
    build_run(conn, "r1", [
        {"metrics": dict(FULL), "lag_ewma_s": health.LAG_CLAMP_MAX_S,
         "lag_max_s": health.LAG_CLAMP_MAX_S},
        {"metrics": dict(FULL), "lag_ewma_s": health.LAG_CLAMP_MAX_S,
         "lag_max_s": health.LAG_CLAMP_MAX_S},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    html = read_html(out)
    assert "≥600s" in html, "saturated lag must be shown as a bound"
    assert "600.000s" not in html, "clamp presented as an exact measurement"
    assert "clamp" in html, "the cap should be explained in nearby help text"


def test_unsaturated_lag_keeps_its_precision(conn, tmp_path):
    build_run(conn, "r1", [
        {"metrics": dict(FULL), "lag_ewma_s": 0.012, "lag_max_s": 0.044},
        {"metrics": dict(FULL), "lag_ewma_s": 0.013, "lag_max_s": 0.051},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    html = read_html(out)
    assert "0.013s" in html or "0.012s" in html
    assert "≥600s" not in html
    assert "clamp" not in html, "no cap note when nothing is saturated"


def test_condition_badges_stay_non_authoritative(report_db, tmp_path):
    """Outline pills, not filled alert chips, and the uncalibrated caveat
    must survive any styling cleanup."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html = read_html(out)
    assert re.search(r"\.pill\s*\{[^}]*border:1px solid currentColor", html)
    assert "not calibrated" in html
    assert "z-score" in html


# --- the three added primitive cards ---------------------------------------

CARD_METRICS = {
    "Unblocks": ("block.delete",),
    "Repost deletes": ("repost.delete",),
    "List mutations": ("listitem.create", "listitem.delete"),
}


def _card_labels(html_doc: str) -> list[str]:
    section = html_doc[html_doc.index("B · Activity weather"):
                       html_doc.index("C · Derived conditions")]
    return re.findall(r'class="metric-name"[^>]*>([^<]+)<', section)


def _card_total(html_doc: str, label: str) -> int:
    section = html_doc[html_doc.index(label):]
    return int(re.search(r"· ([0-9,]+) total", section).group(1).replace(",", ""))


def test_new_cards_read_the_expected_persisted_keys():
    """These must come from keys the classifier already emits — no new
    collection semantics, no invented metric."""
    from weatherwatch.classify import ALLOWED_METRICS
    specs = {label: report._metric_keys((label, m)) for label, m in report.PRIMITIVES}
    for label, expected in CARD_METRICS.items():
        assert specs[label] == expected, f"{label} reads {specs[label]}"
        for key in expected:
            assert key in ALLOWED_METRICS, f"{key} is not a classifier output"


def test_block_delete_maps_to_unblocks(conn, tmp_path):
    build_run(conn, "r1", [
        {"metrics": {"block.create": 9, "block.delete": 4}},
        {"metrics": {"block.create": 7, "block.delete": 3}},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    html_doc = read_html(out)
    assert "Unblocks" in _card_labels(html_doc)
    assert _card_total(html_doc, "Unblocks") == 7
    assert _card_total(html_doc, "Blocks") == 16, "creates must stay separate"


def test_repost_delete_is_distinct_from_repost_create(conn, tmp_path):
    build_run(conn, "r1", [
        {"metrics": {"repost.create": 100, "repost.delete": 6}},
        {"metrics": {"repost.create": 80, "repost.delete": 5}},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    html_doc = read_html(out)
    labels = _card_labels(html_doc)
    assert "Reposts" in labels and "Repost deletes" in labels
    assert _card_total(html_doc, "Reposts") == 180
    assert _card_total(html_doc, "Repost deletes") == 11


def test_list_mutations_is_exactly_create_plus_delete(conn, tmp_path):
    build_run(conn, "r1", [
        {"metrics": {"listitem.create": 12, "listitem.delete": 5}},
        {"metrics": {"listitem.create": 3, "listitem.delete": 8}},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    assert _card_total(read_html(out), "List mutations") == 12 + 5 + 3 + 8

    summary = json.loads((out / "summary.json").read_text())
    # components stay individually available; the sum is presentational only
    assert summary["metrics"]["listitem.create"]["total"] == 15
    assert summary["metrics"]["listitem.delete"]["total"] == 13
    assert "listitem.create+listitem.delete" not in summary["metrics"], (
        "the composite must not be persisted as its own metric"
    )


def test_list_mutations_with_one_side_absent(conn, tmp_path):
    """A window with creates but no deletes is a real zero on that side."""
    build_run(conn, "r1", [
        {"metrics": {"listitem.create": 6}},
        {"metrics": {"listitem.delete": 4}},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    assert _card_total(read_html(out), "List mutations") == 10


def test_list_mutations_with_no_listitem_activity_at_all(conn, tmp_path):
    """Zero everywhere must render as zero, not crash and not vanish."""
    build_run(conn, "r1", [
        {"metrics": {"post.create": 5}},
        {"metrics": {"post.create": 5}},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    html_doc = read_html(out)
    assert "List mutations" in _card_labels(html_doc)
    assert _card_total(html_doc, "List mutations") == 0


def test_sum_series_marks_unobserved_windows(conn):
    """A hole in either component is a hole in the sum, never a partial
    total presented as a whole one."""
    from weatherwatch import query
    build_run(conn, "r1", [
        {"metrics": {"listitem.create": 3, "listitem.delete": 1}},
        None,                                    # UNOBSERVED
        {"metrics": {"listitem.create": 2, "listitem.delete": 2}},
    ])
    a = query.series(conn, ["r1"], "listitem.create")
    b = query.series(conn, ["r1"], "listitem.delete")
    combined = query.sum_series([a, b], "listitem.create+listitem.delete")
    assert [p.count for p in combined.points] == [4, None, 4]
    assert combined.points[1].quality == "unobserved"
    assert combined.total == 8
    assert combined.observed_seconds == 120.0


def test_sum_series_refuses_mismatched_runs(conn):
    from weatherwatch import query
    build_run(conn, "r1", [{"metrics": {"listitem.create": 1}}], start_index=0,
              started_at="2026-01-01T00:00:00+00:00",
              ended_at="2026-01-01T00:01:00+00:00")
    build_run(conn, "r2", [{"metrics": {"listitem.delete": 1}}], start_index=9,
              started_at="2026-01-01T00:09:00+00:00",
              ended_at="2026-01-01T00:10:00+00:00")
    a = query.series(conn, ["r1"], "listitem.create")
    b = query.series(conn, ["r2"], "listitem.delete")
    with pytest.raises(ValueError, match="same runs"):
        query.sum_series([a, b], "x")


def test_activity_weather_has_sixteen_cards(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    labels = _card_labels(read_html(out))
    assert len(labels) == 16, labels
    assert len(report.PRIMITIVES) == 16


def test_card_order_forms_a_four_by_four(report_db, tmp_path):
    """Four columns at desktop width, so display order IS the grid layout."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    labels = _card_labels(read_html(out))
    rows = [labels[i:i + 4] for i in range(0, 16, 4)]
    assert rows[1][2:] == ["Blocks", "Unblocks"], "unblocks sits beside blocks"
    assert "Repost deletes" in rows[2], "repost deletes joins the removals row"
    assert rows[2] == ["Post deletes", "Like deletes", "Repost deletes",
                       "Follow deletes"]
    assert rows[3][0] == "List mutations", "graph churn leads the churn row"


def test_new_cards_carry_no_identity_and_no_new_visual_treatment(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)
    section = html_doc[html_doc.index("B · Activity weather"):
                       html_doc.index("C · Derived conditions")]
    for label, pat in IDENTITY_PATTERNS.items():
        assert not re.search(pat, section, re.I), f"{label} in activity weather"
    # every card is the same markup shape: panel + name + val + one sparkline
    assert section.count('class="panel"') == 16
    assert section.count('class="metric-val"') == 16
    assert section.count('class="spark"') == 16
    assert 'class="metric-unit">/s · ' in section


def test_card_help_text_claims_nothing_about_relationships(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)
    assert "membership churn" in html_doc
    assert "Nothing is inferred about the relationship" in html_doc
    for forbidden in ("unfollowed", "stopped blocking", "reconciled",
                      "relationship ended"):
        assert forbidden not in html_doc.lower()
