"""Timestamp parsing, whose failure modes are all silent.

`to_epoch` returns `None` on anything it cannot read, and every caller treats
that as "no timestamp available" rather than as a parse failure. So a parsing
regression here does not raise, does not log, and does not fail a test that
only checks the happy path — it just quietly removes a check somewhere else.
That is why these assertions are about exact epoch values rather than about
round-tripping, and why the version-specific case has its own test.
"""

from __future__ import annotations

import datetime

from weatherwatch import timeutil

#: 2026-01-02T03:04:05Z, computed rather than round-tripped so the test does
#: not agree with a broken implementation by symmetry.
EPOCH = datetime.datetime(
    2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc).timestamp()


def test_a_z_suffixed_stamp_parses_on_every_supported_python():
    """`datetime.fromisoformat` rejected a trailing `Z` before 3.11, and this
    codebase writes `Z` stamps everywhere — the report, both artifacts, and
    the social observation records. On 3.10 they all parsed to `None`.

    Discovered by CI on 2026-08-25: the social instrument's staleness check
    silently stopped detecting staleness on 3.10 alone, so a stopped station
    would have kept reading as weather. The serving host runs 3.10.
    """
    assert timeutil.to_epoch("2026-01-02T03:04:05Z") == EPOCH
    assert timeutil.to_epoch("2026-01-02T03:04:05z") == EPOCH
    assert timeutil.to_epoch("2026-01-02T03:04:05+00:00") == EPOCH


def test_a_naive_stamp_is_read_as_utc_not_as_local_time():
    """`.timestamp()` on a naive datetime resolves it in the machine's local
    zone. Every stamp this instrument writes is UTC, so reading one as local
    time would shift it by the collector's offset — silently, and by a
    different amount depending on where it runs."""
    assert timeutil.to_epoch("2026-01-02T03:04:05") == EPOCH


def test_an_explicit_offset_is_honoured_not_overwritten():
    """UTC is the default for a stamp that does not say. It is not an
    override for one that does."""
    assert timeutil.to_epoch("2026-01-02T04:04:05+01:00") == EPOCH
    assert timeutil.to_epoch("2026-01-01T22:04:05-05:00") == EPOCH


def test_unparseable_and_empty_input_is_none_not_an_exception():
    for value in (None, "", "   ", "not a timestamp", "2026-13-45T99:99:99Z"):
        assert timeutil.to_epoch(value) is None


def test_the_stamps_this_codebase_writes_round_trip():
    """Whatever the writers emit, the reader must accept. Pinning the pair
    together means changing one and not the other fails here rather than in a
    check that quietly stops running."""
    now = timeutil.now_us()
    assert abs(timeutil.to_epoch(timeutil.us_to_iso(now)) - now / 1e6) < 0.001
    assert abs(timeutil.to_epoch(timeutil.now_iso()) -
               timeutil.now_us() / 1e6) < 5.0
