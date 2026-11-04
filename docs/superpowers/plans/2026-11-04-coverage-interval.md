# Observation coverage interval implementation plan

> **For agentic workers:** Execute inline under the delegated Weatherwatch lane; no further delegation. Use `superpowers:executing-plans` and the already selected debugging, TDD and verification workflow.

**Goal:** Make published observation duration and coverage refer to one physical
stream-time interval, while exposing uncertainty in overlapping run pieces.

**Architecture:** A query-level calculation bounds the union of recorded
durations inside each physical bucket. JSON and both HTML coverage displays use
that calculation. Raw health rows, metric series, baselines and archives remain
the recorded evidence.

**Tech Stack:** Existing Python 3.10-compatible code, SQLite and pytest; no new
dependencies.

**Spec:** [Weatherwatch issue 7](https://github.com/unpingable/atproto-weatherwatch/issues/7),
the observation limits in `../../../README.md` and the public contract in
`../../PUBLIC-ARTIFACTS.md`.

## Global constraints

- No ratio clamp, database rewrite, evidence deletion or inferred continuity.
- Event timestamps are not observation-coverage boundaries.
- Different or overlapping bucket domains cannot be summed independently.
- Preserve recorded counts, flags, gap metadata, archive extent and baselines.
- Child lane is offline only; root owns independent review and deployment.
- Owner delegated routine implementation choices and explicitly continued the
  remaining issues; this plan records those choices without a new design gate.

## Decision and claim

For valid duration pieces `d_i` inside a bucket of width `w`, the union has
lower bound `max(d_i)` and upper bound `min(sum(d_i), w)`. With no observed
piece both are zero. These follow from containment and subadditivity, without
locating any piece. Bounds add only across disjoint physical bucket domains.
Gaps already deducted at collection are not deducted again. Unobserved time
contributes to the span but not to either duration bound. Cursor replay is
metadata, not proof of exact coverage. Exact scalar duration and ratio are
available only when the bounds coincide; otherwise they are null.

This is a read-side claim about the retained duration evidence, not renewed
qualification of historical collection, source completeness or endpoint
coverage. A bounded mathematical argument and enumerated local cases suffice;
there is no new concurrent state transition to model. The owner remains the
decision owner; Codex selects this narrower claim under delegated engineering
judgment.

## Review focus

- Adjacent partial pieces have the same duration-only evidence as overlapping
  partial pieces; neither earns an exact scalar.
- Full-width evidence dominates other valid pieces in its bucket.
- Zero, gaps, unobserved time and replay retain their distinct meaning.
- Negative, over-width and nonfinite durations fail closed before publication.
- Mixed widths, overlapping domains and misaligned grids fail closed.

## Task 1: Correct the query and public consumers

**Files:** Create `tests/test_interval_coverage.py`; modify
`src/weatherwatch/query.py`, `src/weatherwatch/report.py`,
`tests/test_report.py` and `docs/PUBLIC-ARTIFACTS.md`. Pin the actual ambiguous
producer output in `fixtures/coverage-bounded-summary.json` and document its
stable comparison fields beside it for the consuming Facts adapter.

**Interface:** `query.interval_coverage(points: list[WindowPoint])` returns a
small immutable result with physical span, exact optional scalars, lower and
upper bounds, raw recorded-duration sum and counts of split/uncertain buckets.
It rejects invalid duration evidence and incompatible physical bucket domains.

- [ ] Write literal duration cases and real-query/report regressions, including
  a 180-second recorded sum inside a 120-second physical interval.
- [ ] Run focused tests against the unchanged calculation and retain RED.
- [ ] Implement the query calculation with constant working memory over ordered
  points; use it in JSON and both HTML coverage displays.
- [ ] Document the additive fields, null scalars, uncertainty and preserved raw
  window/archive semantics. Update the older exact-split assertion.
- [ ] Run focused GREEN, the complete suite and existing qualification checks;
  retain all failures and distinguish sandbox socket restrictions.
- [ ] Review the diff, commit the isolated branch and write a hashed immutable
  lane completion receipt for root review/integration.
