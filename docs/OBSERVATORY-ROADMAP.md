# Weather Watch observatory roadmap

This is an inventory of research candidates, not a claim that Weather Watch
currently measures them. Each item needs its own source semantics, privacy
boundary, coverage profile, and negative tests before it becomes a public
quantity.

The program asks two different questions:

- **weather:** what did this named observer see happening now?
- **observatory:** what kind of protocol ecosystem is this observation
  becoming consistent with?

Neither question measures attention. **Production observable. Consumption
unobservable.**

## Native, identity-free candidates

These can plausibly remain inside the current finite-counter architecture:

- **Protocol plurality, record share:** syntactically valid public record
  writes under `app.bsky.*` versus every other namespace, using fixed buckets.
  No NSID or authority string may be persisted. This is record share, not app
  adoption: one automated repo can dominate it.
- **Alt-text coverage:** image-bearing post writes with/without non-empty alt
  text, as aggregate boolean counters. This observes record shape, not whether
  assistive technology or any person consumed it.
- **Circadian amplitude:** periodicity in conditioned aggregate rates from one
  named observer. Geography is a possible interpretation, not an observed
  dimension.
- **Conversational topology:** reply/quote shares and any depth/branching
  quantities recoverable without retaining thread identifiers. If topology
  needs a durable thread key, it leaves this tier.
- **Extended observer divergence:** repeated paired probes with separate
  endpoint results. Endpoint observations may be compared; they may not be
  summed into a network total.

Protocol plurality should begin with record share only. Schema breadth,
namespace-authority breadth, persistence, and concentration need longitudinal
state and cardinality protection; one headline percentage cannot stand in for
them.

## Transient identity-aware candidates

These are attractive precisely because they require a new qualified boundary:

- **Approximate active producers:** per-window HyperLogLog over DIDs, with the
  sketch discarded or sealed according to a proven non-recovery policy. “A
  sketch is anonymous” is not accepted as a premise.
- **Regret latency:** create/delete matching requires transient record identity
  and bounded state across potentially long intervals. It is not “just a
  histogram” until retention, eviction, replay, and unmatched-delete semantics
  are proved.
- **PDS transition observation:** the first qualified identity-aware reducer is
  now implemented under [`PLC-REDUCTION.md`](PLC-REDUCTION.md). It exposes
  weekly thresholded endpoint-mutation and migration-like counts without
  provider dimensions. Successful migration, provider concentration/flows,
  live acquisition, and source admission remain candidates, not claims.

Migrant follow-through is explicitly excluded: recognizing that the same
person migrated and later wrote is an actor join, not temporal composition.

## Composition candidates

Reduced native facts may eventually compose with PLC operation aggregates,
normalized search-interest samples, timestamped event corpora, platform status
feeds, Labelwatch, or published research measurements. The contract is in
[`TEMPORAL-COMPOSITION.md`](TEMPORAL-COMPOSITION.md): facts cross source
reducers first, then join by time only, under explicit semantic rules and the
weakest participating coverage envelope.

Deferred without implementation in this closeout: live PLC source admission,
unresolved compositional-disclosure questions, a real composite publication,
and empirical questions that become interesting only after actual use.

## Claims this roadmap does not license

- non-`app.bsky.*` write share is not independent application adoption;
- PLC identity count is not network population or DAU;
- active-producer HLL divided by PLC identities is not a DAU percentage;
- public writes are not attention, impressions, or engagement;
- coincident external events are not causes;
- higher observer volume is not evidence that the observer is complete;
- no constituent measurements will be collapsed into a single
  “decentralization index.”
