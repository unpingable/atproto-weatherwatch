# Repository-declared visibility

Weatherwatch declares its expected operational and epistemic concern surface
at [`.ops/concerns.toml`](../.ops/concerns.toml). The declaration uses the
small, neutral `project.concerns/v1` shape described by
`.ops/concerns.schema.json`; it is repository-owned and names no consumer.

The declaration is an inventory, not evidence. In particular:

```text
declared concern
    != observed concern
    != admitted evidence
    != current qualified observation
```

A generic project view must left-join current observations onto every
`required = true` declaration. A required identifier with no current admitted
observation must remain visible as `MISSING`, `UNKNOWN`, or `UNSATISFIED` in
the consumer's own vocabulary. It must not disappear, and the declaration
must never be substituted for the missing observation.

## Supported local observation interface

The single Weatherwatch diagnostic interface is:

```bash
weatherwatch --db data/weatherwatch.sqlite status \
  --report-dir build/report

weatherwatch --db data/weatherwatch.sqlite status \
  --report-dir build/report --json
```

The first form is the concise operator view. The second emits the shared
`project.ops.status/v1` envelope described by `.ops/status.schema.json`.
Both are projections of the same evaluation; the human output is not a
separate state universe. Weatherwatch-specific privacy custody appears only
under `extensions.weatherwatch`.

Every emitted concern repeats its exact `id`, `question`, and `profile`,
its required flag, the observation time when there is one, and the local facts
used to reach that state. The local state vocabulary is:

| State | Meaning |
|---|---|
| `PRESENT` | Current local facts support the bounded proposition. |
| `DEGRADED` | Current local facts support a proposition-specific bad or conditioned state. |
| `UNKNOWN` | Instrumentation or evidence is insufficient to answer. |
| `STALE` | A fact exists but its proposition-local currentness bound has expired. |
| `ABSENT` | The semantic product or activity has explicitly not yet occurred or is stopped. |
| `REFUSED` | A gate evaluated a candidate and declined it. |

The status command exits successfully when it can render this document.
Non-good concern states are data, not command failures.

## Concern surface and profile semantics

| Concern | Bounded proposition and supporting facts |
|---|---|
| `weatherwatch.observation.coverage` | Present coverage state from the latest instrumented `window_health` row. `warming_up` and a new run without a row are `UNKNOWN`; a gap or coverage gate is `DEGRADED`; an ended run is historical and therefore `ABSENT` as present coverage. |
| `weatherwatch.acquisition.connection` | Fresh identity-free collector runtime testimony says `connected`, `reconnecting`, `disconnected`, `starting`, or `stopped`. This is local socket/loop state, not relay completeness. |
| `weatherwatch.acquisition.cursor` | The endpoint-scoped cursor is supported by the latest durable window. A current explicitly empty window reports `NO_ACTIVITY_OBSERVED`, not a stuck cursor. An old cursor cannot qualify a restarted run that has produced no window. |
| `weatherwatch.acquisition.delay` | Current event-time lag under the threshold already owned by `health.py`. An empty window has no new event-lag sample, so delay is `UNKNOWN` even while coverage and aggregate production remain present. |
| `weatherwatch.observation.loss` | Parse errors, events without stream time, late events, unclassified input, gaps, reconnects, and resume seams from the honesty ledger. The known loss buckets remain mechanically instrumented by `health.py`. |
| `weatherwatch.persistence.access` | The configured SQLite file exists, passes SQLite's read `quick_check`, and permits acquisition of a `BEGIN IMMEDIATE` transaction that is rolled back without application writes. This is narrower than “database healthy” and says nothing about future capacity or recovery. |
| `weatherwatch.persistence.continuity` | Required schema tables/version, integer endpoint-scoped cursor metadata, runtime metadata, atomic cursor/window semantics, and the continued absence of raw-event tables and identity columns. |
| `weatherwatch.aggregate.production` | A `window_health` product exists for the latest run and remains fresh. A present row with `events_seen = 0` is `activity_state = EMPTY`; it is a produced aggregate, not observer blindness. |
| `weatherwatch.report.candidate` | `summary.json` exists and its artifact and source times remain current when evaluated **now**. The recorded build-time freshness label is retained as a fact but cannot refresh the artifact later. |
| `weatherwatch.publication.gate` | The current candidate is complete and contains no identity-shaped value, or is explicitly `REFUSED`, absent, or unreadable. Passing grants no authority and does not say publication happened. |

The acquisition/aggregate profiles bind currentness to the source bucket width
and the existing close grace. Lag uses the existing observation-health
profile. Report-source freshness uses the report's existing “two publication
intervals plus one source bucket” budget; candidate-artifact freshness uses
the existing publication interval. These are semantic profile rules, not
notification thresholds. The manifest contains no recurrence, severity,
page, remediation, or escalation policy.

## Publication gate

`deploy/publish.sh` and status share one implementation:

```bash
weatherwatch publication-gate --report-dir build/report --json
```

Exit `0` means the local candidate is structurally complete and privacy-clean;
exit `2` is a refusal or absence. A refusal reports only the matched shape and
file, never the matching identity bytes. The output always says
`publication_authority: false` and `published: false`. Deployment authority,
transport, and successful serving are intentionally outside this repository
fact.

Candidate output is not authoritative evidence. It is a static, derived
artifact. A candidate can exist while publication is refused; the gate can
pass while the candidate is stale; and neither condition proves the public
site changed.

## Privacy and epistemic boundaries

The visibility work does not change the weather data path. Weatherwatch still
persists only aggregate counters, observation-window health, run metadata,
and endpoint-scoped cursor/meta state. It retains no raw WSS event and adds no
DID, handle, rkey, CID, URI, text, actor, or subject column. Runtime testimony
contains an opaque run ID, the configured endpoint, enum-like connection
state, timestamps, and an exception class name only.

The optional social edge lane remains a separate explicitly enabled bounded
store under its existing custody rules. The visibility document does not read
or expose edge rows and contains no subject-level observation.

The coverage proposition remains exactly “aggregate activity observed from
this Jetstream source during this observation interval.” A monotonic cursor
does not imply completeness. A fresh local WSS connection does not establish
relay completeness or global network truth. An explicit observed-empty window
is not calmness, and a connection with no persisted observation window is not
silently called covered.

## Generic/NQ consumption seam

The locally inspected NQ checkout has versioned witness validation and claim
admission, but it does not currently ship an open generic operational-concern
adapter or a governed-inquiry question kind for this surface. Its inquiry
question enum is closed, and the relevant generic predicate-witness document
is explicitly a candidate rather than a production contract. Weatherwatch
therefore does not emit a made-up `nq.*` packet and this work is not
NQ-integrated.

A future NQ or other generic adapter should:

1. discover `.ops/concerns.toml` and validate `project.concerns/v1`;
2. read the distinct `.ops/observation.toml`
   (`project.observation-binding/v1`) and acquire
   `project.ops.status/v1` through the supported local CLI under Monitor's
   explicit trust and execution policy;
3. reconcile by exact concern, question, and profile identity, retaining the
   producer, acquisition time, exact source artifact, and provenance;
4. retain an explicit `MISSING_REQUIRED_OBSERVATION` inventory gap for every
   required declaration without a matching observation, distinct from a
   producer-emitted `UNKNOWN`;
5. perform NQ admission and semantic qualification without treating the
   declaration or CLI success as evidence for a proposition;
6. let Pulse independently evaluate support/currentness/blindness from the
   current facts and bound profile; and
7. let Nightshift own recurrence, horizon, attention, and escalation.

An admitted historical artifact remains historical. Re-reading its stored
`recorded_freshness_state` must never refresh it; the adapter must use current
facts and the exact semantic profile at the evaluation time.

Monitor now implements discovery, bounded acquisition, structural matching,
and explicit `MISSING_REQUIRED_OBSERVATION` inventory state. It does not
provide generic NQ semantic admission.
