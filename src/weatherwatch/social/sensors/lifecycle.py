"""Tier 2 — account lifecycle transitions alongside observed inbound activity.

ATProto exposes account status transitions as their own event kind, so an
account going away is observable directly rather than inferred from silence,
and `deactivated` / `deleted` / `suspended` / `takendown` are distinguishable.

WHAT THIS DETECTOR CLAIMS
-------------------------
That a status transition was observed at time T, and that inbound activity
toward that repo in [T - lookback, T) was N events from M distinct actors,
against a baseline of B over the preceding comparison interval. That is all.
It is co-occurrence with a stated lookback.

WHAT IT DOES NOT CLAIM
----------------------
Causation. The type string is `deactivation_after_inbound_excess`, not
"deactivation following pressure": "pressure" is an interpretation of the
inbound count, and this instrument observes the count. Three specific
confounders are carried on every finding rather than left to the reader:

* **PDS migration.** Moving hosts deactivates the account on the old PDS. A
  relay is meant to filter the obsolete event, but a `deactivated` transition
  is not by itself evidence that anyone left anything.
* **The lookback is arbitrary.** Any window long enough to catch a reaction is
  long enough to catch unrelated traffic. `lookback_s` is on the envelope.
* **Inbound is undercounted.** The store only sees edges whose collections the
  sink was configured to retain, and only from the moment custody began.

The honest use of this detector is to count how often the co-occurrence
happens at all, against how often deactivations happen without it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ... import timeutil
from ..edges import subject_actor_did
from ..envelope import receipt_hash
from ..scope import AnalysisConfig, EvidenceSet, Finding, Scope, magnitude
from ..store import actor_token, token_salt

DETECTOR_ID = "account_lifecycle_episode"
DETECTOR_VERSION = "v1"

TYPE_DEACTIVATION_AFTER_INBOUND = "deactivation_after_inbound_excess"
TYPE_DEACTIVATION_QUIET = "deactivation_without_inbound_excess"

DEFAULT_LOOKBACK_S = 6 * 3600
DEFAULT_BASELINE_MULTIPLE = 4


@dataclass(frozen=True)
class LifecycleConfig(AnalysisConfig):
    #: Inbound events in the lookback must exceed baseline by this ratio.
    excess_ratio: float = 3.0
    #: ...and be at least this many, so a jump from 0 to 2 is not "3x".
    min_inbound: int = 10
    #: ...from at least this many distinct actors.
    min_distinct_actors: int = 3
    #: Emit the negative case too. Without it the detector only ever produces
    #: confirming instances, which is how a co-occurrence rate gets mistaken
    #: for a mechanism.
    emit_quiet_cases: bool = True


def _status_receipt(row: sqlite3.Row) -> str:
    return receipt_hash({
        "actor": row["actor_did"], "observed_us": row["observed_us"],
        "active": row["active"], "status": row["status"],
    })


def select(
    conn: sqlite3.Connection,
    since_us: int,
    until_us: int,
    lookback_s: int = DEFAULT_LOOKBACK_S,
    baseline_multiple: int = DEFAULT_BASELINE_MULTIPLE,
    source: str = "edge_store",
) -> EvidenceSet:
    """Every inactive-transition in the interval, with its inbound context.

    `lookback_s` and `baseline_multiple` shape *what is looked at*, so they
    live on the Scope and are committed to by `evidence_id` — they are not
    interpretation parameters that could be tuned after seeing the answer.
    """
    lookback_us = lookback_s * 1_000_000
    baseline_us = lookback_us * baseline_multiple

    transitions = conn.execute(
        "SELECT observed_us, actor_did, active, status FROM status_event "
        "WHERE active=0 AND observed_us >= ? AND observed_us < ? "
        "ORDER BY observed_us",
        (since_us, until_us),
    ).fetchall()

    salt = token_salt(conn)
    cases = []
    receipts: list[str] = []

    for row in transitions:
        did, t = row["actor_did"], row["observed_us"]
        receipts.append(_status_receipt(row))

        inbound = conn.execute(
            "SELECT observed_us, actor_did, collection, subject_kind, "
            "subject_ref, rkey, rev, cid FROM edge_event "
            "WHERE observed_us >= ? AND observed_us < ? AND op='create' "
            "AND (subject_ref = ? OR subject_ref LIKE ?)",
            (t - lookback_us, t, did, f"at://{did}/%"),
        ).fetchall()
        base = conn.execute(
            "SELECT COUNT(*) AS n FROM edge_event "
            "WHERE observed_us >= ? AND observed_us < ? AND op='create' "
            "AND (subject_ref = ? OR subject_ref LIKE ?)",
            (t - lookback_us - baseline_us, t - lookback_us, did, f"at://{did}/%"),
        ).fetchone()

        for r in inbound:
            receipts.append(receipt_hash({
                "actor": r["actor_did"], "collection": r["collection"],
                "op": "create", "rkey": r["rkey"], "rev": r["rev"],
                "cid": r["cid"], "subject_kind": r["subject_kind"],
                "subject_ref": r["subject_ref"], "observed_us": r["observed_us"],
            }))

        by_collection: dict[str, int] = {}
        actors: set[str] = set()
        for r in inbound:
            by_collection[r["collection"]] = by_collection.get(r["collection"], 0) + 1
            actors.add(r["actor_did"])

        baseline_n = base["n"] if base else 0
        cases.append({
            "target_token": actor_token(salt, did),
            "transition_us": t,
            "status": row["status"],
            "n_inbound": len(inbound),
            "n_distinct_actors": len(actors),
            "by_collection": dict(sorted(by_collection.items())),
            # Baseline is normalised to the lookback's length so the two are
            # comparable rather than merely adjacent.
            "baseline_inbound": round(baseline_n / baseline_multiple, 3),
            "baseline_raw": baseline_n,
        })

    scope = Scope(
        kind="lifecycle",
        subject_class=f"account.inactive/lookback={lookback_s}s"
                      f"/baseline={baseline_multiple}x",
        ts_start=timeutil.us_to_iso(since_us),
        ts_end=timeutil.us_to_iso(until_us),
        window=f"{lookback_s}s",
        source=source,
    )
    facts = {
        "n_transitions": len(transitions),
        "lookback_s": lookback_s,
        "baseline_multiple": baseline_multiple,
    }
    return EvidenceSet(
        scope=scope, receipts=tuple(receipts), facts=facts, payload=(cases,))


def interpret(
    evidence: EvidenceSet, cfg: LifecycleConfig | None = None,
) -> list[Finding]:
    cfg = cfg or LifecycleConfig()
    if not evidence.payload:
        return []
    cases = evidence.payload[0]
    findings: list[Finding] = []
    n_total = len(cases)

    for c in cases:
        base = c["baseline_inbound"]
        # A zero baseline has no ratio. Falling back to the raw count keeps the
        # case visible, but the number is then a count wearing a ratio's name,
        # so it is flagged rather than left to look like the others.
        undefined_baseline = base <= 0
        ratio = (c["n_inbound"] / base) if not undefined_baseline else (
            float(c["n_inbound"]) if c["n_inbound"] else 0.0)
        qualifies = (
            c["n_inbound"] >= cfg.min_inbound
            and c["n_distinct_actors"] >= cfg.min_distinct_actors
            and ratio >= cfg.excess_ratio
        )
        if not qualifies and not cfg.emit_quiet_cases:
            continue

        t_iso = timeutil.us_to_iso(c["transition_us"])
        explain = {
            "status": c["status"],
            "target_token": c["target_token"],
            "n_inbound_in_lookback": c["n_inbound"],
            "n_distinct_inbound_actors": c["n_distinct_actors"],
            "inbound_by_collection": c["by_collection"],
            "baseline_inbound_normalised": base,
            "excess_ratio": round(ratio, 3),
            "excess_ratio_undefined_baseline": undefined_baseline,
            "lookback_s": evidence.facts["lookback_s"],
            "transitions_in_scope": n_total,
            "confounders": "PDS migration also emits deactivated; inbound is "
                           "limited to retained collections and to time since "
                           "custody began; the lookback is a choice.",
            "note": "Co-occurrence with a stated lookback. Not a causal claim.",
        }
        findings.append(Finding(
            type=(TYPE_DEACTIVATION_AFTER_INBOUND if qualifies
                  else TYPE_DEACTIVATION_QUIET),
            ts_start=t_iso, ts_end=t_iso,
            score=magnitude(ratio) if qualifies else 0.0,
            explain=explain,
            segment_receipts=(receipt_hash({
                "case": c["target_token"], "t": c["transition_us"],
                "status": c["status"], "n_inbound": c["n_inbound"],
            }),),
        ))

    findings.sort(key=lambda f: (f.ts_start, f.type))
    return findings
