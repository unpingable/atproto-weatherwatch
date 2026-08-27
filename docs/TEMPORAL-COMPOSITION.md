# Campaign: TEMPORAL-COMPOSITION-WITHOUT-IDENTITY-LAUNDERING

Status: **Composition V0 core and one source-format PLC reducer implemented;
no live external acquisition, source admission, or real composite publication
claimed.**

The governing rule is structural:

> **Join on the clock, never the actor.**

The compositor accepts already-reduced, bounded facts. It has no actor key,
join-key field, expression language, caller-supplied claim prose, or generic
dimension labels. It cannot fetch Jetstream, Trends, GDELT, a status feed, or
Labelwatch. The PLC reducer is a separate producer and does not fetch its own
source. Source-specific reducers remain responsible for observation and
must cross the reduction boundary before this code can see their facts.

The other boundary belongs on every public product:

> **Production observable. Consumption unobservable.**

ATProto's public streams expose public repository writes and identity/account
events. They do not expose reads, impressions, lurkers, private messages,
reports, client-side mutes, or actual audience. The official protocol material
describes the [repository event stream](https://atproto.com/specs/sync),
[Jetstream](https://atproto.com/guides/streaming-data), and
[NSID syntax](https://atproto.com/specs/nsid). Those source capabilities do not
license an attention or engagement claim.

## Supported interface

```bash
weatherwatch compose --list-rules
weatherwatch compose \
  --input reduced-facts.json \
  --rule weatherwatch.rule.block-label-proximity.v1
```

Input schema: `.ops/composition-bundle.schema.json`,
`weatherwatch.composition.bundle/v1`.

Source contracts live separately in the repository-owned
`.ops/composition-contracts.json`, schema
`weatherwatch.composition.contracts/v1`. A fact bundle can reference those
contracts but cannot carry, amend, or admit its own contract.

Output schema: `.ops/composition-output.schema.json`,
`weatherwatch.composition.output/v1`.

Exit `0` means a candidate composite relation was produced. Exit `2` means
refused or unsatisfied. A composed result explicitly carries
`composition_authority: false` and `source_admission_performed: false`.
Structural validation is not evidence admission, provenance verification, or
publication authority.

## V0 contract

Each repository-declared source contract declares only:

- stable contract and producer identifiers;
- vantage and coverage profiles;
- exact semantic measurements and units;
- finite, enumerated dimensions.

Each fact carries one measurement, one bounded event-time window, the time the
fact was acquired, one explicit validity state, its coverage fraction, value,
unit, and declared dimensions.
Extra fields refuse. Non-current states cannot smuggle historical values into
a present composition.

Contract dimensions must select from vocabularies installed in code; a source
cannot invent `cohort=alice` merely by listing it in its contract. Actor-, account-, DID-, handle-,
host-, repo-, URI-, subject-, token-, hash-, or pseudonym-shaped dimensions
refuse. Identity-shaped values and opaque stable hexadecimal identifiers
refuse without being echoed in the refusal. This is defense in depth: the
stronger property is that composition code contains no dimension-based join.

## Installed rules

V0 installs three exact semantic rules:

| rule | relation | permits | refuses |
|---|---|---|---|
| `production-search-association.v1` | Pearson correlation across at least three exactly aligned windows | descriptive association | attention, engagement, audience, causation |
| `block-label-proximity.v1` | bounded temporal overlap | proximity | either direction of causation, intent, coordination |
| `production-plc-proximity.v1` | bounded temporal overlap | proximity | population, DAU, per-capita production, migrant follow-through |

There is no rule language. A request for `writes × Trends = engagement`, a
Jetstream network total, or `HLL / PLC identities = DAU` dies as
`RULE_NOT_INSTALLED`.

## Campaign ledger

### C0 — Source contract: implemented for V0

Closed registry/fact shapes, time windows, semantic profiles, units, coverage,
states, and installed finite dimensions validate before composition.
Caller-supplied contracts, malformed facts, and under-specified inputs refuse.

### C1 — Clock-only join: implemented for V0

Overlap is calculated only as `max(start) < min(end)`. Correlation requires
identical start and end times; the engine does not rebin, interpolate, or guess.
No identity or generic join key exists in the model.

### C2 — Coverage intersection: implemented for V0

Composite state is the weakest participating state. Composite coverage is the
minimum participating fraction, never the union. Missing is not zero,
degraded is not quiet, and two partial observations remain partial.

### C3 — Semantic compatibility: implemented for installed rules

Facts must match their source contract's measurement and unit. Only installed
measurement pairs and operations can compose. Arbitrary arithmetic refuses.

### C4 — Temporal relations: partial

Simultaneous overlap and exact-window descriptive Pearson correlation are
implemented. Lead/lag and before/during/after require separate explicit rules
and qualification. The machinery emits no causal conclusion.

### C5 — Claim projection: implemented for V0

Every output carries exact input fact snapshots, propagated coverage, permitted
and forbidden interpretations, the installed proposition, and authority
refusals. Claim sentences are fixed by reviewed rules, never supplied by input.

### C6 — PLC reduction: implemented; live admission deliberately absent

The source-format reducer described in [`PLC-REDUCTION.md`](PLC-REDUCTION.md)
accepts only the official sequenced export shape. Raw DIDs and service endpoints
exist only in a batch-local map which is cleared before return; the output has
no identity, provider, pseudonym, or generic dimension field. Weekly
non-overlapping windows, a minimum disclosure count, zero/small-cell
equivalence, a bounded output horizon, separate event/acquisition time, and
non-echoing crash custody are adversarially tested. Migrant follow-through,
population denominators, provider flows, successful migration, live source
acquisition, cryptographic verification, and source admission remain outside
the contract.

The threshold statement is per fact. Arithmetic across subset/superset facts
or repeated acquisitions can reveal a small complement or revision delta;
compositional non-disclosure is explicitly not qualified. This does not create
an identity field or actor join in persisted output.

### C7 — External-source hostility: deliberately unimplemented

Trends normalization, event-corpus selection, status-feed revision, clock
error, granularity drift, partial disappearance, and historical revision each
need a source-specific reducer and coverage profile. A generic external metric
table is rejected as epistemic laundering infrastructure.

### C8 — Cross-instrument composition: seam only

The block/label rule exists, but no Driftwatch or Labelwatch adapter is claimed.
“Label issuance and block writes overlapped” is permitted once externally
admitted facts
exist. “Moderation caused blocking” remains mechanically forbidden.

### C9 — Adversarial epistemic laundering: implemented for V0

Tests attempt identity labels, pseudonyms, hashes, stale values, missing-as-zero,
partial-envelope union, duplicate facts, unit substitution, guessed alignment,
network-total summation, attention/engagement promotion, and fake DAU. They
refuse or remain explicitly unsatisfied.

### C10 — Publication artifact: deliberately unimplemented

No real composite report is published because no second production source has
been reduced and admitted. A fixture-backed demonstration would not justify
calling the system externally composed. The first report must use real admitted
facts and receive a prose review specifically looking for sentences that outrun
their evidence.

## Non-goals

- no actor cohorts or migrant follow-through;
- no generic or live network ingestion; raw PLC rows enter only the qualified
  source-specific batch reducer and cannot enter the compositor;
- no stable pseudonymous join keys;
- no generic `source_metric` database;
- no automatic causal language;
- no recurrence, notification, or escalation policy;
- no claim that structurally valid input is admitted evidence.
