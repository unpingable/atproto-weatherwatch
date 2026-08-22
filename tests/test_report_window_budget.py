"""Regression: the report must survive its own history growing.

Continuous 60s collection crossed 20,000 span windows on 2026-08-22 and
`weatherwatch-publish` began failing with QueryTooLarge. The guard is right --
silently truncating a series is worse than refusing -- but it was written for
accidental unbounded queries, and the report's range is deliberate.
"""

from __future__ import annotations

import pytest

from weatherwatch import query, report

from .conftest import build_run


def test_report_budget_exceeds_the_default_guard():
    assert report.REPORT_MAX_WINDOWS > 20_000


def test_total_events_series_honours_an_explicit_budget(conn):
    build_run(conn, "run-b", [{"metrics": {"post.create": 1}} for _ in range(12)])
    with pytest.raises(query.QueryTooLarge):
        query.total_events_series(conn, ["run-b"], max_points=5)
    s = query.total_events_series(conn, ["run-b"], max_points=50)
    assert len(s.points) == 12


def test_report_renders_past_the_default_guard(conn, tmp_path, monkeypatch):
    """A span wider than the default cap must still render.

    Simulated by shrinking the default rather than writing 20,000 rows: the
    property under test is that the report passes its own budget, not the
    arithmetic of the cap.
    """
    build_run(conn, "run-c", [{"metrics": {"post.create": 3}} for _ in range(40)])
    monkeypatch.setattr(query, "series", _budget_probe(query.series, 10))
    out = report.generate_report(conn, tmp_path / "site")
    assert (tmp_path / "site" / "index.html").exists()
    assert out["windows"] == 40


def _budget_probe(real, floor):
    """Wrap `series` so anything asking for the DEFAULT cap gets a tiny one."""
    def wrapped(conn, run_ids, metric, *a, max_points=20_000, **kw):
        if max_points == 20_000:
            max_points = floor          # an un-budgeted caller would now fail
        return real(conn, run_ids, metric, *a, max_points=max_points, **kw)
    return wrapped
