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

## Composition candidates, ranked

The contract is in [`TEMPORAL-COMPOSITION.md`](TEMPORAL-COMPOSITION.md): facts
cross source reducers first, then join by time only, under explicit semantic
rules and the weakest participating coverage envelope. The dividing rule for
admitting anything new:

> New sources should primarily explain coverage, bound measurement error, or
> provide timestamped context. They must not introduce actor joins or silently
> convert correlation into explanation.

Ranked — filing order, not authorization:

- **P1 — Standing second Jetstream observer.** Turn the single paired probe
  into a continuous observer-divergence measurement: observed activity plus a
  divergence envelope, instead of one stream posing as truth. Streams are
  never summed; disagreement is a coverage signal, and the read layer keeps
  refusing cross-observer combination. Observer-side rather than a composed
  source; extends the extended-observer-divergence candidate above.
- **P2 — Infrastructure and status annotations.** Relay, PDS, and service
  incidents joined by clock, with provenance. Distinguishes "activity fell"
  from "the window onto activity degraded". Annotation only; never "this
  outage caused that activity change".
- **P3 — Protocol and release annotations.** Lexicon additions, relay and
  PDS software releases, app releases, collector-relevant upstream changes.
  Explains structural discontinuities — including the `untracked.collection`
  and `unclassified.*` canaries — without attributing motives to users.
- **P4 — PLC aggregate composition.** The reducer is already qualified;
  compare reduced weekly PLC facts with reduced activity facts under the
  installed `production-plc-proximity.v1` rule. No actor joins, provider
  narratives, or migration-success claims.
- **Parking lot — external event corpora and search-interest samples.**
  Interesting but epistemically expensive: they import keyword selection,
  geographic sampling, opaque normalization, and a "what counts as attention"
  decision, and they measure the consumption side this instrument
  structurally refuses to claim. If ever admitted, they are annotations with
  dates ("Aug 14 — major outage"), never detections ("Weatherwatch detected
  reaction to …"). Labelwatch/Driftwatch facts and published research
  measurements wait here too; the C8 seam in the composition contract is
  their entry point if either is ever reduced and admitted.

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
