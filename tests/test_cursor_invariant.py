"""The commit invariant:

    persisted_cursor = greatest time_us whose contribution is durably
                       represented in the SAME transaction

Everything else about restart correctness follows from this one property.
"""

from __future__ import annotations

import pytest

from weatherwatch import db
from weatherwatch.accumulator import Accumulator
from weatherwatch.classify import Classification

WIDTH = 60
BASE = 1_700_000_040  # a real 60s bucket boundary (1_700_000_000 is not)


def ev(offset_s: float, metric: str = "post.create") -> Classification:
    return Classification(
        time_us=int((BASE + offset_s) * 1_000_000), metrics=(metric,)
    )


def _run(conn, run_id="run-test-000000", endpoint="wss://relay.invalid/x"):
    db.start_run(conn, run_id, endpoint, "test", WIDTH, "2026-01-01T00:00:00Z",
                 None, None)
    return run_id, endpoint


def test_cursor_equals_greatest_committed_time_us(conn):
    run_id, endpoint = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    for off in (1, 2, 3):
        acc.observe(ev(off))
    acc.observe(ev(61))  # crosses into the next window, closing the first

    closed = acc.take_closed()
    assert len(closed) == 1
    cursor = Accumulator.commit_cursor_for(closed)
    assert cursor == int((BASE + 3) * 1_000_000)

    db.flush_windows(conn, run_id, endpoint, closed, cursor)
    assert db.get_cursor(conn, endpoint) == cursor


def test_cursor_never_advances_past_committed_data(conn):
    run_id, endpoint = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.observe(ev(61))
    closed = acc.take_closed()
    too_far = int((BASE + 61) * 1_000_000)
    with pytest.raises(ValueError, match="exceeds greatest committed"):
        db.flush_windows(conn, run_id, endpoint, closed, too_far)
    assert db.get_cursor(conn, endpoint) is None


def test_cursor_cannot_advance_with_no_committed_windows(conn):
    _, endpoint = _run(conn)
    with pytest.raises(ValueError, match="no committed windows"):
        db.flush_windows(conn, "run-test-000000", endpoint, [], 12345)


def test_empty_window_does_not_advance_the_cursor(conn):
    """An empty observed window proves we were watching. It does not prove
    the stream advanced, so it must not push the cursor past unseen events."""
    run_id, endpoint = _run(conn)
    fake_now = {"v": int((BASE + 2) * 1_000_000)}
    acc = Accumulator(run_id, bucket_width=WIDTH, grace_s=5,
                      now_us=lambda: fake_now["v"])
    acc.observe(ev(1))
    fake_now["v"] = int((BASE + 200) * 1_000_000)  # stream went quiet
    acc.tick()  # wall clock has moved well past this window
    closed = acc.take_closed()
    assert closed
    cursor = Accumulator.commit_cursor_for(closed)
    assert cursor == int((BASE + 1) * 1_000_000)

    empty_only = [w for w in closed if w.events_seen == 0]
    if empty_only:
        assert Accumulator.commit_cursor_for(empty_only) is None


def test_crash_before_flush_leaves_cursor_unmoved(conn):
    """Counts accumulated but never flushed are simply gone. The cursor did
    not move, so a restart replays exactly that interval."""
    run_id, endpoint = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    for off in (1, 2, 3):
        acc.observe(ev(off))
    # process dies here — no flush
    assert db.get_cursor(conn, endpoint) is None
    assert conn.execute("SELECT COUNT(*) c FROM bucket").fetchone()["c"] == 0


def test_crash_after_flush_does_not_recount_committed_events(conn):
    """Restart resumes at cursor+1, which M0 proved excludes the event at
    the cursor exactly. The committed window is not re-observed."""
    run_id, endpoint = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    for off in (1, 2, 3):
        acc.observe(ev(off))
    acc.observe(ev(61))
    closed = acc.take_closed()
    cursor = Accumulator.commit_cursor_for(closed)
    db.flush_windows(conn, run_id, endpoint, closed, cursor)

    committed = conn.execute(
        "SELECT count FROM bucket WHERE metric='post.create'"
    ).fetchone()["count"]
    assert committed == 3

    # Restart: a new run resumes at cursor+1.
    resume_from = db.get_cursor(conn, endpoint) + 1
    assert resume_from == int((BASE + 3) * 1_000_000) + 1

    replayed = [e for e in (ev(1), ev(2), ev(3), ev(61)) if e.time_us >= resume_from]
    assert [e.time_us for e in replayed] == [int((BASE + 61) * 1_000_000)]

    run2 = "run-test-000001"
    db.start_run(conn, run2, endpoint, "test", WIDTH, "2026-01-01T00:01:00Z",
                 None, resume_from)
    acc2 = Accumulator(run2, bucket_width=WIDTH)
    for e in replayed:
        acc2.observe(e)
    acc2.close_for_shutdown()
    closed2 = acc2.take_closed()
    db.flush_windows(conn, run2, endpoint, closed2,
                     Accumulator.commit_cursor_for(closed2))

    total = conn.execute(
        "SELECT SUM(count) t FROM bucket WHERE metric='post.create'"
    ).fetchone()["t"]
    assert total == 4, "each event counted exactly once across the restart"


def test_uncommitted_window_is_replayed_and_reconstructed(conn):
    """Crash mid-window: the partial window was never committed, so the
    restart rebuilds it from the start of the uncommitted interval."""
    run_id, endpoint = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.observe(ev(61))          # closes window 1
    acc.observe(ev(62))          # window 2, uncommitted
    acc.observe(ev(63))
    closed = acc.take_closed()   # only window 1
    assert len(closed) == 1
    db.flush_windows(conn, run_id, endpoint, closed,
                     Accumulator.commit_cursor_for(closed))
    # crash. window 2's three events are gone from memory.

    resume_from = db.get_cursor(conn, endpoint) + 1
    replayed = [e for e in (ev(61), ev(62), ev(63)) if e.time_us >= resume_from]
    assert len(replayed) == 3, "the whole uncommitted window replays"

    run2 = "run-test-000002"
    db.start_run(conn, run2, endpoint, "test", WIDTH, "2026-01-01T00:01:00Z",
                 None, resume_from)
    acc2 = Accumulator(run2, bucket_width=WIDTH)
    for e in replayed:
        acc2.observe(e)
    acc2.close_for_shutdown()
    c2 = acc2.take_closed()
    db.flush_windows(conn, run2, endpoint, c2, Accumulator.commit_cursor_for(c2))

    total = conn.execute(
        "SELECT SUM(count) t FROM bucket WHERE metric='post.create'"
    ).fetchone()["t"]
    assert total == 4


def test_double_flush_of_a_window_raises_rather_than_double_counting(conn):
    run_id, endpoint = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.observe(ev(61))
    closed = acc.take_closed()
    cursor = Accumulator.commit_cursor_for(closed)
    db.flush_windows(conn, run_id, endpoint, closed, cursor)
    with pytest.raises(db.FlushIntegrityError):
        db.flush_windows(conn, run_id, endpoint, closed, cursor)


def test_cursor_is_per_endpoint(conn):
    run_id, ep_a = _run(conn, endpoint="wss://a.invalid/x")
    acc = Accumulator(run_id, bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.observe(ev(61))
    closed = acc.take_closed()
    db.flush_windows(conn, run_id, ep_a, closed,
                     Accumulator.commit_cursor_for(closed))

    assert db.get_cursor(conn, ep_a) is not None
    assert db.get_cursor(conn, "wss://b.invalid/x") is None, (
        "a cursor from one relay must be meaningless against another"
    )


def test_late_event_does_not_mutate_a_committed_window(conn):
    run_id, _ = _run(conn)
    acc = Accumulator(run_id, bucket_width=WIDTH)
    acc.observe(ev(1))
    acc.observe(ev(61))   # closes window 1
    acc.observe(ev(2))    # late arrival for window 1
    acc.observe(ev(121))  # closes window 2
    closed = {w.bucket_start: w for w in acc.take_closed()}
    first = closed[BASE]
    assert first.events_seen == 1, "committed window untouched"
    assert closed[BASE + WIDTH].late_events == 1, "loss recorded, not hidden"
