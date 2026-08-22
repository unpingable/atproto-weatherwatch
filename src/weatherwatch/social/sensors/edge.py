"""Tier 2 — concentration, overlap and synchronisation over the edge store.

These are the detectors the counter lane cannot express. Each is a structural
statement about a *set of observed records*, and each terminates in receipts:

* **actor concentration** — is the activity coming from few repos or many?
* **target concentration** — is it landing on few subjects or many?
* **cohort overlap** — do distinct actors produce near-identical target sets?
* **temporal synchronisation** — how compressed in time are their first acts?

None of them names a mechanism. High overlap is consistent with a shared
blocklist, with independent reactions to one prominent target, and with
coincidence at low n; the detector reports the overlap and the base rate and
stops there. The word "coordinated" appears in no type string in this module,
because it is a claim about intent and this instrument cannot observe intent.

BASE RATE
---------
Overlap on a very popular target is not evidence of anything: if 40,000
accounts block the same widely-blocked repo, every pair of them shares a
target. Each finding therefore carries `target_prevalence` — the share of
in-scope actors touching the episode's most common target — so the trivial
explanation is visible next to the number rather than buried under it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ... import timeutil
from ..edges import subject_actor_did
from ..scope import AnalysisConfig, EvidenceSet, Finding, Scope, magnitude
from ..store import actor_token, event_receipt, token_salt
from ..edges import EdgeEvent

DETECTOR_ID = "edge_structure_episode"
DETECTOR_VERSION = "v1"

TYPE_ACTOR_CONCENTRATION = "actor_concentration"
TYPE_TARGET_CONCENTRATION = "target_concentration"
TYPE_COHORT_OVERLAP = "cohort_overlap"
TYPE_SYNCHRONISATION = "temporal_synchronisation"

#: Pairwise Jaccard is O(n^2). Above this many eligible actors the sensor
#: refuses rather than silently sampling: a quietly truncated overlap number
#: is worse than no overlap number, because it looks complete.
MAX_OVERLAP_ACTORS = 1_500


class ScopeTooLarge(ValueError):
    """Raised instead of silently sampling."""


@dataclass(frozen=True)
class EdgeConfig(AnalysisConfig):
    min_events: int = 20
    #: Herfindahl above which a distribution is called concentrated. 1/n is
    #: uniform; 1.0 is a monopoly.
    h_threshold: float = 0.15
    #: Jaccard at or above which two actors' target sets are called similar.
    jaccard_threshold: float = 0.5
    #: An actor needs this many distinct targets before overlap means anything.
    min_actor_targets: int = 3
    #: A cohort needs this many actors.
    min_cohort_actors: int = 3
    #: Synchronisation: first-acts inside this span are eligible...
    sync_span_s: float = 3600.0
    #: ...but must also be this many times more compressed than the interval
    #: they were drawn from. Without this, any group of actors observed inside
    #: a one-hour scope trivially "synchronises" within one hour, and every
    #: ordinary hour on the network becomes an episode.
    min_compression: float = 10.0
    min_sync_actors: int = 5


def _herfindahl(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts)


def load_edges(
    conn: sqlite3.Connection, collection: str, since_us: int, until_us: int,
    op: str = "create",
) -> list[EdgeEvent]:
    rows = conn.execute(
        "SELECT observed_us, actor_did, collection, op, subject_kind, "
        "subject_ref, rkey, rev, cid, record_created_at FROM edge_event "
        "WHERE collection=? AND op=? AND observed_us >= ? AND observed_us < ? "
        "ORDER BY observed_us",
        (collection, op, since_us, until_us),
    ).fetchall()
    return [EdgeEvent(**dict(r)) for r in rows]


def select(
    conn: sqlite3.Connection,
    collection: str,
    since_us: int,
    until_us: int,
    op: str = "create",
    source: str = "edge_store",
) -> EvidenceSet:
    """Every edge of one collection in one interval. No thresholds."""
    events = load_edges(conn, collection, since_us, until_us, op)
    scope = Scope(
        kind="edge",
        subject_class=f"{collection}.{op}",
        ts_start=timeutil.us_to_iso(since_us),
        ts_end=timeutil.us_to_iso(until_us),
        window=f"{round((until_us - since_us) / 1_000_000)}s",
        source=source,
    )
    receipts = tuple(event_receipt(e) for e in events)

    actor_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    for e in events:
        actor_counts[e.actor_did] = actor_counts.get(e.actor_did, 0) + 1
        tgt = e.subject_ref or ""
        if tgt:
            target_counts[tgt] = target_counts.get(tgt, 0) + 1

    facts = {
        "n_events": len(events),
        "n_actors": len(actor_counts),
        "n_targets": len(target_counts),
        "actor_herfindahl": round(_herfindahl(list(actor_counts.values())), 6),
        "target_herfindahl": round(_herfindahl(list(target_counts.values())), 6),
        "span_s": round((until_us - since_us) / 1_000_000, 3),
    }
    salt = token_salt(conn)
    return EvidenceSet(
        scope=scope, receipts=receipts, facts=facts,
        payload=(events, salt),
    )


def _actor_target_sets(events: list[EdgeEvent]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in events:
        if not e.subject_ref:
            continue
        # A like/repost target is an AT-URI; reduce it to the target repo so
        # "these actors all hit the same account" is visible even when they
        # hit different posts of it.
        key = subject_actor_did(e.subject_kind, e.subject_ref) or e.subject_ref
        out.setdefault(e.actor_did, set()).add(key)
    return out


def _components(adj: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    comps: list[list[str]] = []
    for node in sorted(adj):
        if node in seen:
            continue
        stack, comp = [node], []
        seen.add(node)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        comps.append(sorted(comp))
    return comps


def _receipts_for(events: list[EdgeEvent], actors: set[str]) -> tuple[str, ...]:
    return tuple(event_receipt(e) for e in events if e.actor_did in actors)


def interpret(
    evidence: EvidenceSet, cfg: EdgeConfig | None = None,
) -> list[Finding]:
    cfg = cfg or EdgeConfig()
    if not evidence.payload:
        return []
    events, salt = evidence.payload
    if len(events) < cfg.min_events:
        return []

    f = evidence.facts
    ts_start, ts_end = evidence.scope.ts_start, evidence.scope.ts_end
    findings: list[Finding] = []
    all_receipts = tuple(evidence.receipts)

    # -- concentration ------------------------------------------------------
    n_events = f["n_events"]
    for htype, key, label in (
        (TYPE_ACTOR_CONCENTRATION, "actor_herfindahl", "actors"),
        (TYPE_TARGET_CONCENTRATION, "target_herfindahl", "targets"),
    ):
        h = f[key]
        n = f["n_actors"] if label == "actors" else f["n_targets"]
        if h < cfg.h_threshold or not n:
            continue
        # The uniform reference is one event per participant, not one event
        # per *observed* participant: normalising by the observed count would
        # score "60 events, 1 target" and "1 event, 1 target" identically,
        # when only the first is a concentration.
        uniform = 1.0 / n_events if n_events else 1.0
        findings.append(Finding(
            type=htype,
            ts_start=ts_start, ts_end=ts_end,
            score=magnitude(h / uniform),
            explain={
                "collection": evidence.scope.subject_class,
                "herfindahl": h,
                "uniform_baseline": round(uniform, 6),
                "effective_n": round(1.0 / h, 3) if h else None,
                f"n_{label}": n,
                "n_events": n_events,
                "note": "Herfindahl over the observed distribution. "
                        "effective_n is 1/H: the number of equally-active "
                        "participants that would produce the same figure.",
            },
            segment_receipts=all_receipts,
        ))

    # -- overlap ------------------------------------------------------------
    sets = {a: t for a, t in _actor_target_sets(events).items()
            if len(t) >= cfg.min_actor_targets}
    if len(sets) > MAX_OVERLAP_ACTORS:
        raise ScopeTooLarge(
            f"{len(sets)} eligible actors exceeds MAX_OVERLAP_ACTORS="
            f"{MAX_OVERLAP_ACTORS}; narrow the interval rather than sampling"
        )

    cohorts: list[list[str]] = []
    if len(sets) >= cfg.min_cohort_actors:
        actors = sorted(sets)
        adj: dict[str, set[str]] = {a: set() for a in actors}
        pair_scores: dict[tuple[str, str], float] = {}
        for i, a in enumerate(actors):
            for b in actors[i + 1:]:
                sa, sb = sets[a], sets[b]
                inter = len(sa & sb)
                if not inter:
                    continue
                j = inter / len(sa | sb)
                if j >= cfg.jaccard_threshold:
                    adj[a].add(b)
                    adj[b].add(a)
                    pair_scores[(a, b)] = j
        for comp in _components(adj):
            if len(comp) < cfg.min_cohort_actors:
                continue
            cohorts.append(comp)
            members = set(comp)
            js = [v for (a, b), v in pair_scores.items()
                  if a in members and b in members]
            shared = set.intersection(*(sets[a] for a in comp))
            # Base rate: how ordinary is this cohort's most common target?
            tally: dict[str, int] = {}
            for a in sets:
                for t in sets[a]:
                    tally[t] = tally.get(t, 0) + 1
            prevalence = (
                max((tally[t] for t in shared), default=0) / max(len(sets), 1)
            )
            findings.append(Finding(
                type=TYPE_COHORT_OVERLAP,
                ts_start=ts_start, ts_end=ts_end,
                # Effective cohort size: how many actors would have to be
                # perfectly aligned to produce this much observed alignment.
                score=magnitude(
                    (sum(js) / len(js) if js else 0.0) * len(comp)),
                explain={
                    "collection": evidence.scope.subject_class,
                    "n_actors": len(comp),
                    "mean_jaccard": round(sum(js) / len(js), 4) if js else 0.0,
                    "n_shared_targets": len(shared),
                    "target_prevalence": round(prevalence, 4),
                    "actor_tokens": [actor_token(salt, a) for a in comp],
                    "n_eligible_actors_in_scope": len(sets),
                    "note": "Target-set similarity only. Consistent with a "
                            "shared list, with independent reaction to a "
                            "common target, and with coincidence at low n.",
                },
                segment_receipts=_receipts_for(events, members),
            ))

    # -- synchronisation ----------------------------------------------------
    first_act: dict[str, int] = {}
    for e in events:
        if e.actor_did not in first_act or e.observed_us < first_act[e.actor_did]:
            first_act[e.actor_did] = e.observed_us

    groups: list[list[str]] = cohorts or ([sorted(first_act)] if first_act else [])
    for group in groups:
        times = sorted(first_act[a] for a in group if a in first_act)
        if len(times) < cfg.min_sync_actors:
            continue
        span_s = (times[-1] - times[0]) / 1_000_000
        scope_span = evidence.facts["span_s"] or 1.0
        if span_s > cfg.sync_span_s:
            continue
        # Compression relative to the interval the actors could have spread
        # over. 1.0 means "no more compressed than the window itself", which
        # is the null result, not a finding.
        compression = min(
            scope_span / span_s if span_s > 0 else scope_span * 1e6, 1e6)
        if compression < cfg.min_compression:
            continue
        findings.append(Finding(
            type=TYPE_SYNCHRONISATION,
            ts_start=timeutil.us_to_iso(times[0]),
            ts_end=timeutil.us_to_iso(times[-1]),
            score=magnitude(compression),
            explain={
                "collection": evidence.scope.subject_class,
                "n_actors": len(times),
                "first_act_span_s": round(span_s, 3),
                "scope_span_s": scope_span,
                "compression": round(compression, 4),
                "actor_tokens": [actor_token(salt, a) for a in sorted(group)],
                "note": "Temporal compression of first observed action. "
                        "Says nothing about why the actions coincided.",
            },
            segment_receipts=_receipts_for(events, set(group)),
        ))

    findings.sort(key=lambda x: (x.ts_start, x.type))
    return findings
