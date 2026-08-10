"""How much statistical power does a cross-system lag analysis have TODAY?

Read-only, prints only aggregates, persists nothing. Re-run this before
attempting the nblocklist (or labelwatch) cross-analysis to see whether enough
Weatherwatch history has accumulated to make it worth doing.

    /opt/weatherwatch/.venv/bin/python analysis/power_check.py [DB_PATH]

The headline problem is not sample count, it is autocorrelation. Minute-to-
minute platform activity is strongly self-correlated, so 1,686 eligible
minutes are worth far fewer independent observations than they appear. Testing
121 lags against an effective N in the low hundreds will produce an
impressive-looking peak from noise alone.
"""

from __future__ import annotations

import sys

from weatherwatch import db, query

DEFAULT_DB = "/var/lib/weatherwatch/weatherwatch.sqlite"
ENDPOINT = "wss://jetstream1.us-east.bsky.network/subscribe"

#: Representative primitives, chosen to span the autocorrelation range.
METRICS = ("block.create", "block.delete", "post.create.quote",
           "follow.delete", "post.delete", "listitem.create")

#: A ±60m sweep at 1-minute resolution.
LAGS_TESTED = 121
#: Rough target: enough independent observations that 121 lags are not simply
#: a fishing expedition. Not a formal power calculation.
TARGET_NEFF = 2000


def lag1_autocorrelation(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    den = sum((x - m) ** 2 for x in xs)
    if not den:
        return None
    return sum((xs[i] - m) * (xs[i + 1] - m) for i in range(n - 1)) / den


def effective_n(n: int, r: float) -> float:
    """AR(1) effective sample size. Strongly autocorrelated series carry far
    less independent information than their length suggests."""
    return n * (1 - r) / (1 + r) if r < 1 else 0.0


def main(path: str = DEFAULT_DB) -> int:
    conn = db.connect(path)
    runs = query.compatible_runs(conn, ENDPOINT)
    if not runs:
        print("no observation runs")
        return 1

    print(f"{'metric':<22}{'N_elig':>8}{'lag1_r':>9}{'N_eff':>9}   days_for_target")
    worst_ratio = 1.0
    for metric in METRICS:
        s = query.series(conn, runs, metric)
        xs = [p.rate for p in s.points if p.baseline_eligible and p.rate is not None]
        r = lag1_autocorrelation(xs)
        if r is None:
            continue
        neff = effective_n(len(xs), r)
        ratio = neff / len(xs) if xs else 1.0
        worst_ratio = min(worst_ratio, ratio) if ratio else worst_ratio
        days = (TARGET_NEFF / ratio) / 60 / 24 if ratio else float("inf")
        print(f"{metric:<22}{len(xs):>8}{r:>9.3f}{neff:>9.0f}   {days:>6.1f}")

    s = query.series(conn, runs, "block.create")
    elig = sum(1 for p in s.points if p.baseline_eligible)
    hours = elig / 60
    print()
    print(f"eligible minutes        : {elig} ({hours:.1f}h)")
    print(f"hour-of-week cells      : {min(int(hours), 168)}/168 covered "
          f"({'matching impossible' if hours < 168 else 'ok'})")
    print(f"non-overlapping 90m windows : {int(hours * 60 // 90)}")
    print(f"lags tested at +/-60m/1min  : {LAGS_TESTED}")
    print()
    print(f"For the most autocorrelated metric, ~{TARGET_NEFF / worst_ratio / 60 / 24:.0f} "
          f"days of continuous collection would be needed to reach "
          f"N_eff={TARGET_NEFF}.")
    print("Hour-of-week controls need >=168h regardless, realistically 4-8 weeks "
          "for more than one sample per cell.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB))
