"""Observation-run semantics and the partial/empty/unobserved distinction.

    outside a run                => NOT OBSERVED   (no row)
    inside healthy run           => OBSERVED       (row, events_seen >= 0)
    inside degraded run          => OBSERVED/CONDITIONED (coverage_state)
    missing interval inside run  => GAP            (gap_us, resume_seam)
"""

from __future__ import annotations

import pytest

from weatherwatch import db, read
from weatherwatch.accumulator import Accumulator
from weatherwatch.classify import Classification

WIDTH = 60
BASE = 1_700_000_040  # a real 60s bucket boundary (1_700_000_000 is not)
EP_A = "wss://a.invalid/subscribe"
EP_B = "wss://b.invalid/subscribe"


def ev(offset_s: float) -> Classification:
    return Classification(int((BASE + offset_s) * 1_000_000), ("post.create",))


def mkrun(conn, run_id, endpoint=EP_A, started="2026-01-01T00:00:00Z"):
    db.start_run(conn, run_id, endpoint, "test", WIDTH, started, None, None)


def flush(conn, acc, run_id, endpoint=EP_A):
    closed = acc.take_closed()
    if not closed:
        return []
    for w in closed:
        w.coverage_state = "ok"
    db.flush_windows(conn, run_id, endpoint, closed,
                     Accumulator.commit_cursor_for(closed))
    return closed


# --- partial vs full vs empty ---------------------------------------------

def test_partial_first_window_is_marked_partial(conn):
    """Joining mid-window must not masquerade as a full observation."""
    mkrun(conn, "r1")
    acc = Accumulator("r1", bucket_width=WIDTH)
    acc.observe(ev(30))   # half-way into the window
    acc.observe(ev(61))
    closed = flush(conn, acc, "r1")
    first = [w for w in closed if w.bucket_start == BASE][0]
    assert first.partial is True
    assert first.observed_duration_us < WIDTH * 1_000_000
    assert first.observed_duration_us == pytest.approx(30 * 1_000_000, rel=0.01)


def test_partial_last_window_is_marked_partial(conn):
    mkrun(conn, "r2")
    acc = Accumulator("r2", bucket_width=WIDTH)
    acc.observe(ev(0))
    acc.observe(ev(61))
    acc.observe(ev(80))
    acc.close_for_shutdown()
    closed = {w.bucket_start: w for w in flush(conn, acc, "r2")}
    assert closed[BASE].partial is False
    assert closed[BASE + WIDTH].partial is True


def test_full_window_is_not_partial(conn):
    mkrun(conn, "r3")
    acc = Accumulator("r3", bucket_width=WIDTH)
    acc.observe(ev(0))
    acc.observe(ev(59))
    acc.observe(ev(61))
    closed = {w.bucket_start: w for w in flush(conn, acc, "r3")}
    assert closed[BASE].partial is False
    assert closed[BASE].observed_duration_us == WIDTH * 1_000_000


def test_observed_empty_window_differs_from_unobserved_time(conn):
    """The central distinction. A present row with events_seen=0 means we
    watched and nothing happened. An absent row means nobody was watching."""
    mkrun(conn, "r4")
    now = {"v": int((BASE + 1) * 1_000_000)}
    acc = Accumulator("r4", bucket_width=WIDTH, grace_s=5, now_us=lambda: now["v"])
    acc.observe(ev(0))
    now["v"] = int((BASE + 200) * 1_000_000)  # stream went quiet
    acc.tick()
    flush(conn, acc, "r4")

    rows = conn.execute(
        "SELECT bucket_start, events_seen FROM window_health "
        "WHERE run_id='r4' ORDER BY bucket_start"
    ).fetchall()
    starts = {r["bucket_start"]: r["events_seen"] for r in rows}

    assert starts.get(BASE) == 1, "observed, non-empty"
    assert starts.get(BASE + WIDTH) == 0, "observed, empty — row must exist"
    # A window nobody observed has no row at all, which is a different fact
    # from a zero count.
    assert (BASE + 10 * WIDTH) not in starts


def test_empty_observed_window_is_full_coverage_not_a_gap(conn):
    mkrun(conn, "r5")
    now = {"v": int((BASE + 1) * 1_000_000)}
    acc = Accumulator("r5", bucket_width=WIDTH, grace_s=5, now_us=lambda: now["v"])
    acc.observe(ev(0))
    now["v"] = int((BASE + 200) * 1_000_000)  # stream went quiet
    acc.tick()
    closed = {w.bucket_start: w for w in flush(conn, acc, "r5")}
    empty = closed[BASE + WIDTH]
    assert empty.events_seen == 0
    assert empty.gap_us == 0
    assert empty.partial is False
    assert empty.observed_duration_us == WIDTH * 1_000_000


# --- gaps and seams --------------------------------------------------------

def test_reconnect_gap_is_recorded_even_though_time_us_stays_monotonic(conn):
    """M0's survey lost ~5,000 events across a live reconnect while time_us
    stayed strictly increasing (198,249/198,249 forward). Ordering cannot
    detect the hole; the seam has to be recorded structurally."""
    mkrun(conn, "r6")
    acc = Accumulator("r6", bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.note_reconnect(gap_us=30_000_000, resume_seam=True)
    acc.observe(ev(40))   # strictly later than ev(1): monotonic, yet a gap
    acc.observe(ev(61))
    closed = {w.bucket_start: w for w in flush(conn, acc, "r6")}
    w = closed[BASE]
    assert w.gap_us == 30_000_000
    assert w.resume_seam is True
    assert w.reconnects == 1
    assert w.partial is True, "a gap must reduce observed coverage"
    # Observation began 1s into this window (first window of the run), so the
    # span is 59s, and the 30s gap comes off that. Both deductions are real
    # and neither is allowed to be rounded away.
    assert w.observed_duration_us == 59_000_000 - 30_000_000


def test_discard_open_window_preserves_the_seam(conn):
    """Reconnect replay rebuilds the counts but must not erase the fact that
    a reconnect happened, or the replay would look like clean observation."""
    acc = Accumulator("r7", bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.note_reconnect(gap_us=5_000_000, resume_seam=True)
    acc.discard_open_window()
    assert acc.take_closed() == [], "nothing committed"
    acc.observe(ev(1))   # replayed
    acc.observe(ev(61))
    closed = {w.bucket_start: w for w in acc.take_closed()}
    w = closed[BASE]
    assert w.resume_seam is True
    assert w.gap_us == 5_000_000
    assert w.counts["post.create"] == 1, "replay reconstructed, not doubled"


# --- run separation --------------------------------------------------------

def test_endpoint_change_creates_a_hard_seam(conn):
    """A run binds to one endpoint. Switching relays starts a new run and
    cannot inherit the previous cursor."""
    mkrun(conn, "rA", endpoint=EP_A)
    acc = Accumulator("rA", bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.observe(ev(61))
    flush(conn, acc, "rA", EP_A)
    assert db.get_cursor(conn, EP_A) is not None
    assert db.get_cursor(conn, EP_B) is None

    mkrun(conn, "rB", endpoint=EP_B)
    rows = conn.execute("SELECT run_id, source_endpoint FROM observation_run "
                        "ORDER BY run_id").fetchall()
    assert [(r["run_id"], r["source_endpoint"]) for r in rows] == [
        ("rA", EP_A), ("rB", EP_B)]


def test_runs_from_different_endpoints_cannot_be_summed(conn):
    for rid, ep in (("rA", EP_A), ("rB", EP_B)):
        mkrun(conn, rid, endpoint=ep)
        acc = Accumulator(rid, bucket_width=WIDTH)
        acc.observe(ev(1))
        acc.observe(ev(61))
        flush(conn, acc, rid, ep)
        db.end_run(conn, rid, "2026-01-01T00:02:00Z", "test",
                   int((BASE + 1) * 1e6), int((BASE + 61) * 1e6))

    with pytest.raises(read.NotSummable, match="different endpoints"):
        read.assert_summable(conn, ["rA", "rB"])
    with pytest.raises(read.NotSummable):
        read.metric_series(conn, ["rA", "rB"], "post.create")


def test_overlapping_runs_cannot_be_summed(conn):
    for rid, lo, hi in (("r1", 0, 120), ("r2", 60, 180)):
        mkrun(conn, rid, endpoint=EP_A)
        db.end_run(conn, rid, "2026-01-01T00:05:00Z", "test",
                   int((BASE + lo) * 1e6), int((BASE + hi) * 1e6))
    with pytest.raises(read.NotSummable, match="overlapping"):
        read.assert_summable(conn, ["r1", "r2"])


def test_sequential_same_endpoint_runs_may_be_summed(conn):
    """A collector restarted across a reboot produces consecutive pieces of
    one timeline. Those do sum."""
    for rid, lo, hi in (("r1", 0, 120), ("r2", 130, 240)):
        mkrun(conn, rid, endpoint=EP_A)
        acc = Accumulator(rid, bucket_width=WIDTH)
        acc.observe(ev(lo + 1))
        acc.observe(ev(lo + 61))
        acc.close_for_shutdown()
        flush(conn, acc, rid, EP_A)
        db.end_run(conn, rid, "2026-01-01T00:05:00Z", "test",
                   int((BASE + lo) * 1e6), int((BASE + hi) * 1e6))
    read.assert_summable(conn, ["r1", "r2"])
    series = read.metric_series(conn, ["r1", "r2"], "post.create")
    assert sum(r["count"] for r in series) == 4


def test_bucket_rows_are_attributed_to_their_run(conn):
    """run_id in the primary key is what makes accidental summation
    impossible rather than merely discouraged."""
    for rid in ("r1", "r2"):
        mkrun(conn, rid, endpoint=EP_A)
        acc = Accumulator(rid, bucket_width=WIDTH)
        acc.observe(ev(1))
        acc.observe(ev(61))
        flush(conn, acc, rid, EP_A)
    rows = conn.execute(
        "SELECT run_id, count FROM bucket WHERE bucket_start=? AND metric=?",
        (BASE, "post.create"),
    ).fetchall()
    assert len(rows) == 2 and {r["run_id"] for r in rows} == {"r1", "r2"}


def test_run_coverage_reports_observed_below_nominal_when_partial(conn):
    mkrun(conn, "r1")
    acc = Accumulator("r1", bucket_width=WIDTH)
    acc.observe(ev(30))
    acc.observe(ev(61))
    acc.close_for_shutdown()
    flush(conn, acc, "r1")
    cov = read.run_coverage(conn, "r1")
    assert cov["partial_windows"] >= 1
    assert cov["observed_duration_us"] < cov["nominal_duration_us"]

