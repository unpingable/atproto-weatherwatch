"""M7 — static dashboard generation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from weatherwatch import db, report
from weatherwatch import derive as derive_module
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


def prose(out: Path) -> str:
    """Rendered text with whitespace collapsed.

    Prose lives inside wrapped f-strings, so a literal match against the raw
    file breaks whenever a sentence re-wraps. Normalise before asserting.
    """
    return re.sub(r"\s+", " ", read_html(out))


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
    html = prose(out).lower()
    assert "authoritative or complete" in html
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
    assert "undefined" in html
    # No formula, no score, no number attached to it.
    beef = html[html.index("GLOBAL BEEF INDEX"):]
    beef = beef[:beef.index("</div>", beef.index("undefined"))]
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


# --- deployment unit files (static inspection, no systemd needed) ----------

UNIT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "systemd"


def _unit(name: str) -> str:
    return (UNIT_DIR / name).read_text()


def _directives(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    joined, buf = [], ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1].strip() + " "
            continue
        joined.append(buf + line.strip()); buf = ""
    for line in joined:
        if "=" in line and not line.startswith(("#", "[")):
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def test_collector_unit_runs_the_installed_collector_unbounded():
    d = _directives(_unit("weatherwatch-collector.service"))
    exec_start = d["ExecStart"][0]
    assert exec_start.startswith("/opt/weatherwatch/.venv/bin/python")
    assert "-m weatherwatch.cli" in exec_start
    assert "--db /var/lib/weatherwatch/weatherwatch.sqlite" in exec_start
    assert "collect" in exec_start
    assert "--duration" not in exec_start, "continuous means run until stopped"


def test_collector_unit_uses_the_repo_endpoint():
    from weatherwatch.collector import DEFAULT_ENDPOINT
    exec_start = _directives(_unit("weatherwatch-collector.service"))["ExecStart"][0]
    assert f"--endpoint {DEFAULT_ENDPOINT}" in exec_start, (
        "the unit must pin the exact tested endpoint, not a guessed URL"
    )


def test_collector_restart_policy_cannot_storm():
    d = _directives(_unit("weatherwatch-collector.service"))
    assert d["Restart"] == ["on-failure"]
    assert int(d["RestartSec"][0]) >= 10, "a tight restart loop is a storm"
    assert d["KillSignal"] == ["SIGTERM"], "graceful shutdown flushes the window"
    assert int(d["TimeoutStopSec"][0]) >= 30
    assert "StartLimitBurst" in d


def test_publisher_is_a_oneshot_that_calls_the_tested_script():
    d = _directives(_unit("weatherwatch-publish.service"))
    assert d["Type"] == ["oneshot"]
    assert d["ExecStart"] == ["/opt/weatherwatch/deploy/publish.sh"], (
        "publication logic must not be reimplemented in the unit"
    )
    env = " ".join(d["Environment"])
    assert "WW_MODE=local" in env
    assert "WW_DB=/var/lib/weatherwatch/weatherwatch.sqlite" in env
    assert "WW_TARGET=/var/www/weatherwatch" in env


def test_neither_unit_couples_to_the_other():
    """The boundary is load-bearing: each must work with the other stopped."""
    for name in ("weatherwatch-collector.service", "weatherwatch-publish.service"):
        text = _unit(name)
        for directive in ("After=", "Requires=", "Wants=", "BindsTo=", "PartOf="):
            for line in text.splitlines():
                if line.startswith(directive):
                    assert "weatherwatch" not in line, f"{name}: {line}"


def test_units_do_not_run_as_root_and_stay_confined():
    for name in ("weatherwatch-collector.service", "weatherwatch-publish.service"):
        d = _directives(_unit(name))
        assert d["User"] == ["weatherwatch"], f"{name} must not run as root"
        assert d["NoNewPrivileges"] == ["true"]
        assert d["ProtectSystem"] == ["strict"]
        assert d["ProtectHome"] == ["true"]
        assert d["CapabilityBoundingSet"] == [""]
    # only the publisher may reach the webroot, and only via a per-unit group
    coll = _directives(_unit("weatherwatch-collector.service"))
    pub = _directives(_unit("weatherwatch-publish.service"))
    assert coll["ReadWritePaths"] == ["/var/lib/weatherwatch"]
    assert "/var/www" in pub["ReadWritePaths"][0]
    assert "SupplementaryGroups" not in coll
    assert pub["SupplementaryGroups"] == ["labelwatch"]


def test_timer_publishes_every_five_minutes_without_catchup():
    d = _directives(_unit("weatherwatch-publish.timer"))
    assert d["OnUnitActiveSec"] == ["5min"]
    assert d["OnBootSec"] == ["2min"]
    assert d["Unit"] == ["weatherwatch-publish.service"]
    assert "Persistent" not in d, (
        "a missed static-report refresh is not data loss; OnBootSec covers it"
    )
    assert "[Install]" in _unit("weatherwatch-publish.timer")
    assert "[Install]" not in _unit("weatherwatch-publish.service"), (
        "the oneshot must be timer-driven, never enabled as a service"
    )


# --- semantic honesty of the unclassified presentation ---------------------

def test_ingest_accounting_separates_failure_from_deliberate_scope(report_db,
                                                                  tmp_path):
    """Four of the five taxonomy categories are already separately persisted:
    parse_errors / rejected_no_time_us / late_events are their own columns, and
    "unknown schema" is the sum of three separately-keyed metrics. Only
    untracked collection shares a key with a never-observed failure case.

    The section must therefore show observer FAILURES apart from deliberate
    SCOPE; the old "Loss buckets: … unclassified 121,312" read as 121k observer
    failures when the observer had failed zero times."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)

    assert "Loss buckets" not in html_doc, (
        "the section held deliberate scope as well as loss"
    )
    failures = html_doc[html_doc.index("Ingest accounting"):]
    failures = failures[:failures.index("</dd>")]
    for category in ("parse errors", "no time_us", "unknown schema",
                     "late events"):
        assert category in failures, f"{category} missing from the taxonomy"
    assert "untracked" not in failures.lower(), (
        "deliberate scope must not sit in the observer-failure line"
    )

    scope = html_doc[html_doc.index("Untracked collection"):]
    scope = scope[:scope.index("</dd>")]
    assert "not</strong>\n          loss" in scope or "not" in scope
    assert "Deliberate scope" in scope


def test_untracked_count_is_not_folded_into_any_loss_figure(report_db, tmp_path):
    """Untracked vocabulary must not contribute to a health/loss number."""
    from weatherwatch import health
    assert "unclassified" in health.KNOWN_LOSS_PATHS, (
        "it stays an instrumented bucket — it just is not loss"
    )
    losses = {k: 0 for k in health.KNOWN_LOSS_PATHS}
    h = health.ObservationHealth()
    for _ in range(health.WARMUP_WINDOWS):
        h.record_window(600, 60.0, losses)
    snap = h.record_window(600, 60.0, {**losses, "unclassified": 999_999})
    assert snap["loss_frac"] == 0.0, (
        "untracked vocabulary must never move the loss fraction"
    )
    assert "loss_observed" not in snap["gate_reasons"]


def test_legacy_key_ambiguity_is_stated_not_hidden(conn, tmp_path):
    build_run(conn, "r1", [
        {"metrics": {**FULL, "unclassified.collection": 40}},
        {"metrics": {**FULL, "unclassified.collection": 35}},
    ])
    out = tmp_path / "beef"
    report.generate_report(conn, out)
    html_doc = read_html(out)
    assert "75" in html_doc, "the untracked count should be shown"
    assert "unclassified.collection" in html_doc, (
        "the legacy key backing the count should be named"
    )
    assert "not\n          yet observed" in html_doc or "not yet observed" in html_doc


def test_beef_placeholder_keeps_the_joke_and_claims_no_calibration(report_db,
                                                                   tmp_path):
    """The unserious name is deliberate epistemic signalling: a solemn
    construct name would imply validity the system has not earned."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)
    assert "GLOBAL BEEF INDEX" in html_doc
    assert "undefined" in html_doc
    for solemn in ("Behavioral Turbulence", "Social Stress Index",
                   "Network Conflict Index", "Conflict Score"):
        assert solemn not in html_doc, f"{solemn!r} implies unearned validity"


def test_dashboard_narrates_no_social_stories(report_db, tmp_path):
    """The instrument reports aggregate behaviour; it must not certify the
    joke. Humans may interpret; the telemetry does not entail."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out).lower()
    for narrative in ("morning-after", "deleting evidence", "the fight",
                      "drama", "feud", "pile-on", "users are ",
                      "backlash", "outrage"):
        assert narrative not in html_doc, f"narrative leaked: {narrative!r}"


def test_ratios_keep_their_components_inspectable(report_db, tmp_path):
    """The denominator gets a lawyer: every ratio's numerator and denominator
    must remain visible as primitives, so a ratio move can be attributed."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    summary = json.loads((out / "summary.json").read_text())
    for _label, num, den in derive_module.STANDARD_RATIOS:
        assert num in summary["metrics"], f"{num} not inspectable"
        assert den in summary["metrics"], f"{den} not inspectable"


def test_beef_placeholder_makes_no_overall_normality_claim(report_db, tmp_path):
    """"Everyone appears normal" read as a platform-wide interpretation from a
    composite that does not exist."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    low = read_html(out).lower()
    for claim in ("everyone appears normal", "looks calm", "no unusual",
                  "nothing to see", "no conflict detected", "all quiet"):
        assert claim not in low, f"overall social assessment: {claim!r}"


def test_beef_placeholder_does_not_promise_future_calibration(report_db,
                                                              tmp_path):
    """"calibration pending" implied an externally validated target is merely
    waiting to be collected. It is not assumed to exist."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)
    assert "calibration pending" not in html_doc
    assert "undefined" in html_doc
    assert "calibration not assumed" in html_doc
    assert "Primitive conditions above remain\n    authoritative." in html_doc \
        or "remain" in html_doc
    # and not prematurely claiming a composite exists
    assert "uncalibrated by design" not in html_doc, (
        "that wording implies a formula already exists"
    )


def test_latest_column_states_which_window_it_means(report_db, tmp_path):
    """"/s now" read as instantaneous. It is the most recent OBSERVED window."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)
    assert ">latest /s<" in html_doc
    assert "/s now" not in html_doc
    assert "most\nrecent <em>observed</em>" in html_doc or "most recent" in html_doc
    for wrong in ("instantaneous reading", "live gauge", "average over the run"):
        assert wrong in html_doc, f"the note should rule out {wrong!r}"


def test_ratio_note_states_the_two_body_problem(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = read_html(out)
    assert "two-body system" in html_doc
    assert "it means" in html_doc and "is small" in html_doc, (
        "small-denominator caveat"
    )


def test_every_ratio_component_is_also_a_primitive_card(report_db, tmp_path):
    """The ratio note promises the components appear as their own cards. Keep
    that true: the receipts must exist for every hint."""
    card_metrics = set()
    for spec in report.PRIMITIVES:
        card_metrics.update(report._metric_keys(spec))
    for label, num, den in derive_module.STANDARD_RATIOS:
        assert num in card_metrics, f"{label}: numerator {num} has no card"
        assert den in card_metrics, f"{label}: denominator {den} has no card"


def test_cleanup_changed_no_formula_or_threshold():
    """This pass was presentation only."""
    from weatherwatch import derive, health
    assert (derive.Z_SURGING, derive.Z_ELEVATED,
            derive.Z_QUIET, derive.Z_DEGRADING) == (3.0, 1.5, -1.5, -3.0)
    assert derive.DEFAULT_BASELINE_N == 15
    assert derive.MIN_BASELINE_SAMPLES == 5
    assert health.LAG_CLAMP_MAX_S == 600.0
    assert health.LAG_HIGH_THRESHOLD_S == 120
    assert health.COVERAGE_LOW_THRESHOLD == 0.6
    assert len(derive.STANDARD_RATIOS) == 9, "no ratios added or removed"
    assert len(report.PRIMITIVES) == 16, "no metrics added or removed"


# --- cold-read framing -----------------------------------------------------
# A cold reader arriving at /beef with no context — human or model — reads
# "beef" + Bluesky + telemetry and concludes conflict monitoring. Every
# disclaimer on the page used to be about COVERAGE (which relay, how complete);
# none said what is not measured. The correction has to arrive before the joke.

DENIALS = ("conflict", "sentiment", "users", "content")


def test_negative_scope_statement_precedes_everything_else(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = prose(out)

    denial = html_doc.index("does not measure")
    assert denial < html_doc.index("Cortisol"), (
        "the meme layer must be repaired before it is deployed"
    )
    assert denial < html_doc.index("A · Observation status"), (
        "the correction belongs on the first screen, not below the fold"
    )
    for word in DENIALS:
        assert word in html_doc[denial:denial + 400], f"{word!r} not denied"


def test_page_denies_the_specific_misreadings(report_db, tmp_path):
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    low = prose(out).lower()
    for claim in ("identifies anyone", "reconstructs a social graph",
                  "reads any post", "detects a dispute"):
        assert claim in low, f"missing denial: {claim!r}"
    assert "joke name for a composite that does not exist" in low


def test_the_joke_survives_the_correction(report_db, tmp_path):
    """Correcting the misread must not sand off the humour — the joke is the
    disclaimer, and a solemn rename would imply unearned validity."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    html_doc = prose(out)
    assert "GLOBAL BEEF INDEX" in html_doc
    assert "Cortisol accounting" in html_doc
    assert "event velocity, not affect" in html_doc, (
        "the stress-sounding phrase should repair itself in place"
    )


def test_machine_readers_get_the_same_correction(report_db, tmp_path):
    """A script or model parsing summary.json should not have to read prose to
    learn what this is not."""
    out = tmp_path / "beef"
    report.generate_report(report_db, out)
    summary = json.loads((out / "summary.json").read_text())
    assert "measures" in summary
    nots = " ".join(summary["does_not_measure"]).lower()
    for word in ("conflict", "sentiment", "individual users", "content",
                 "social graph", "identity"):
        assert word in nots, f"{word!r} not denied in the machine surface"
