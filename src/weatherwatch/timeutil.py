"""Time helpers.

Two clocks, never conflated:

* **stream clock** — Jetstream's ``time_us``, microseconds. Relay-observed.
  This is the ONLY clock used for window assignment. M0 verified it strictly
  increasing with no ties over 198,249 consecutive transitions.
* **wall clock** — this host. Used for run bookkeeping, for lag measurement,
  and to close windows while the stream is silent. Never for assignment.

``record.createdAt`` is a third clock and is never read. It is
producer-controlled; driftwatch has production rows stamped year 2999. Claimed
time is testimony; observed time is custody.
"""

from __future__ import annotations

import datetime


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def now_us() -> int:
    """Wall clock in microseconds, comparable to Jetstream ``time_us``."""
    return int(now_utc().timestamp() * 1_000_000)


def us_to_iso(time_us: int) -> str:
    return datetime.datetime.fromtimestamp(
        time_us / 1_000_000, tz=datetime.timezone.utc
    ).isoformat()


def bucket_start_for(time_us: int, width_s: int) -> int:
    """Window this stream timestamp belongs to, as unix seconds.

    Truncation, not rounding: a bucket covers [start, start + width).
    """
    return (int(time_us) // 1_000_000 // width_s) * width_s


def to_epoch(iso: str | None) -> float | None:
    """Parse an ISO8601 stamp we wrote ourselves back to unix seconds."""
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def parse_duration(text: str) -> float:
    """Parse '30m', '1h', '600s', '90' (bare = seconds) into seconds."""
    t = text.strip().lower()
    if not t:
        raise ValueError("empty duration")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if t[-1] in units:
        value, mult = t[:-1], units[t[-1]]
    else:
        value, mult = t, 1
    try:
        seconds = float(value) * mult
    except ValueError as exc:
        raise ValueError(f"bad duration {text!r}") from exc
    if seconds <= 0:
        raise ValueError(f"duration must be positive, got {text!r}")
    return seconds
