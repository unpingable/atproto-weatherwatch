"""Deduct only the union of missing intervals inside the observed span."""

from __future__ import annotations

import pytest

from weatherwatch.accumulator import Accumulator
from weatherwatch.classify import Classification

BASE = 1_700_000_040
US = 1_000_000


def event(offset):
    return Classification((BASE + offset) * US, ("post.create",))


def gap(acc, start, end):
    acc.note_reconnect(
        gap_us=(end - start) * US,
        gap_end_us=(BASE + end) * US,
        resume_seam=True,
    )


@pytest.mark.parametrize("intervals, observed_us, ledger_us", [
    ([(-10, 5)], 50_000_000, 15_000_000),
    ([(-10, 20)], 40_000_000, 30_000_000),
    ([(20, 30)], 40_000_000, 10_000_000),
    ([(50, 80)], 40_000_000, 30_000_000),
    ([(20, 35), (30, 40)], 30_000_000, 25_000_000),
    ([(20, 25), (40, 45)], 40_000_000, 10_000_000),
])
def test_only_gap_overlap_reduces_observation(intervals, observed_us, ledger_us):
    acc = Accumulator("gap-span")
    acc.observe(event(10))
    for start, end in intervals:
        gap(acc, start, end)
    acc.observe(event(max(61, max(end for _, end in intervals))))
    window = acc.take_closed()[0]
    assert window.observed_duration_us == observed_us
    assert window.gap_us == ledger_us
    assert window.resume_seam
    assert window.partial


def test_replay_preserves_gap_bounds_and_does_not_deduct_overlap_twice():
    acc = Accumulator("gap-replay")
    acc.observe(event(10))
    gap(acc, 20, 40)
    acc.discard_open_window()
    acc.observe(event(10))
    gap(acc, 30, 50)
    acc.observe(event(61))
    window = acc.take_closed()[0]
    assert window.events_seen == 1
    assert window.observed_duration_us == 20_000_000
    assert window.gap_us == 40_000_000
    assert window.reconnects == 2
    assert window.resume_seam


def test_shutdown_clips_gap_to_the_last_observed_event():
    acc = Accumulator("gap-shutdown")
    acc.observe(event(10))
    gap(acc, 40, 70)
    acc.observe(event(50))
    acc.close_for_shutdown()
    window = acc.take_closed()[0]
    assert window.observed_duration_us == 30_000_000
    assert window.gap_us == 30_000_000
    assert window.partial


def test_duration_only_signal_remains_conservative_alongside_known_bounds():
    acc = Accumulator("gap-unlocated")
    acc.observe(event(10))
    gap(acc, 20, 30)
    acc.note_reconnect(gap_us=5_000_000, resume_seam=True)
    acc.observe(event(61))
    window = acc.take_closed()[0]
    assert window.observed_duration_us == 35_000_000
    assert window.gap_us == 15_000_000
