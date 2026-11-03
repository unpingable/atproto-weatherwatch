"""Window finalization requires stream progress, including after a stall.

Silence has no source completeness watermark. A quiet upstream and a wedged
connection therefore have the same conservative result until events resume.
"""

from __future__ import annotations

import pytest

from weatherwatch.accumulator import Accumulator
from weatherwatch.classify import Classification

WIDTH = 60
BASE = 1_700_000_040
US = 1_000_000


def ev(seconds):
    return Classification(int((BASE + seconds) * US), ("post.create",))


def feed(acc, clock, seconds, lag_s=0):
    for offset in seconds:
        clock[0] = int((BASE + offset + lag_s) * US)
        acc.observe(ev(offset))
        acc.tick()


def started_accumulator():
    clock = [BASE * US]
    acc = Accumulator("finalization-test", now_us=lambda: clock[0])
    feed(acc, clock, [-59, -30, 1])
    acc.take_closed()  # the preceding minute provides a committed replay basis
    return acc, clock


def test_silence_waits_for_stream_progress_without_inventing_empty_windows():
    acc, clock = started_accumulator()
    feed(acc, clock, [2, 3])
    clock[0] = (BASE + 3 * WIDTH + 6) * US
    acc.tick()
    assert acc.take_closed() == []

    # Only the window with actual observations can now be finalized. There
    # is no evidence that the intervening minutes were observed and empty.
    feed(acc, clock, [181])
    closed = acc.take_closed()
    assert len(closed) == 1
    assert closed[0].bucket_start == BASE
    assert closed[0].events_seen == 3
    assert closed[0].observed_duration_us == WIDTH * US
    assert not closed[0].partial


def test_genuine_catch_up_never_runs_ahead_of_stream_time():
    acc, clock = started_accumulator()
    feed(acc, clock, [i * 0.5 for i in range(3, 400)], lag_s=600)
    closed = acc.take_closed()
    assert all(w.late_events == 0 for w in closed)
    assert sum(w.events_seen for w in closed) + acc._open.events_seen == 398


def test_replay_behind_committed_high_water_is_counted_without_reopening():
    acc, clock = started_accumulator()
    feed(acc, clock, [30, 61])
    first = acc.take_closed()
    assert first[0].events_seen == 2
    acc.discard_open_window()
    feed(acc, clock, [1, 30, 61, 62, 121])
    closed = acc.take_closed()
    assert len(closed) == 1
    assert closed[0].bucket_start == BASE + WIDTH
    assert closed[0].events_seen == 2
    assert closed[0].late_events == 2
    assert first[0].events_seen == 2


@pytest.mark.parametrize("reconnect", [False, True])
def test_a_stall_does_not_finalize_complete_observation_before_replay(reconnect):
    acc, clock = started_accumulator()
    feed(acc, clock, [20, 40])
    clock[0] = (BASE + WIDTH + 6) * US  # stall passes end + old five-second grace
    acc.tick()
    assert acc.take_closed() == []

    if reconnect:
        acc.discard_open_window()
        # Resume from the durable cursor in the preceding minute, not from
        # the last received event in this uncommitted minute.
        feed(acc, clock, [1, 20, 40], lag_s=30)
    feed(acc, clock, [41, 50, 59, 61], lag_s=30)
    closed = acc.take_closed()
    assert len(closed) == 1
    window = closed[0]
    assert window.bucket_start == BASE
    assert window.events_seen == window.counts["post.create"] == 6
    assert window.late_events == 0
    assert window.observed_duration_us == WIDTH * US
    assert not window.partial
    assert window.resume_seam is reconnect
