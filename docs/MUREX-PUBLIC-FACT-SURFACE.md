# Campaign: Murex / public-readonly-instrument-fact-api

Track: public-observation-surface
Codename: **Murex**
Canonical slug: **public-readonly-instrument-fact-api**

Status: **reconnaissance complete; contract specified; implementation not
performed.**

Implementation decision: **B — SHARED-SURFACE-DESIGN-ONLY**
Result classification: **shared envelope sustained for two instruments,
disproved as a three-instrument surface at this time; prerequisites named.**

Campaign identity and result classification are separate. This document is the
campaign record; it confers no publication authority and does not assert that
anything was published.

Repositories examined:

| repository | branch at examination | HEAD at examination |
|---|---|---|
| `atproto-weatherwatch` | `main` | `a80eb04` |
| `atproto-labelwatch` | `main` | `04e5068` |
| `atproto-driftwatch` | `incident/2026-08-12-volume-exhaustion` | `6525e5e` |

These are the commits each repository was *sitting on* when Murex read it.
Murex produced no commits of its own; it changed no code. The follow-up
campaign **Breakwater** (`observation-adequacy-and-public-boundary-hardening`)
is what committed this record and the repairs it identified — see §12.

Labelwatch and Driftwatch were examined with unrelated in-flight worktree
changes present (an operations-visibility workstream). Those changes were read
but not modified, staged, reverted, or relied upon.

---

## 1. Executive result

The thesis — *one boring, deliberately anemic public fact surface rather than
three unrelated APIs* — survives contact with the repositories, but not in the
shape the brief proposed, and not yet for three instruments.

Three findings drive the decision.

**The estate already owns the right envelope, and it is better than the one
proposed.** Weatherwatch's `weatherwatch.composition.bundle/v1`
(`.ops/composition-bundle.schema.json`) is a reduced-fact interchange format
carrying exactly one measurement per fact with its own bounded window,
acquisition time, validity state, coverage fraction, unit and enumerated
dimensions. It was built as a compositor *input* format. It is the correct
public *output* format, essentially unchanged. Building a parallel
`/api/v1/<instrument>/` envelope beside it would create the second drifting
contract the brief forbids.

**The proposed envelope's `coverage` and `window` fields are placed wrong.**
The brief's illustrative envelope hangs one `window` and one `coverage` block
off the document. That is honest for Weatherwatch, whose coverage is a single
time fraction over a single stream. It is *not* honest for Labelwatch, whose
coverage is a per-labeler poll-success ratio with no defensible estate-wide
scalar. Per-fact coverage — which the existing bundle schema already
requires — is the honest shape. Section 5 details this.

**Driftwatch's publication boundary is written but not enforced.**

> **Correction (Breakwater, 2026-08-28).** Murex originally recorded that
> Driftwatch had *no* intentional publication boundary. That was wrong, and the
> error was one of reading rather than judgement: Driftwatch carries
> `docs/architecture/PUBLIC_SURFACES.md` and
> `docs/architecture/diagrams/publication-boundary.md`, which define a surfaces
> inventory, stage gating, an aggregate / per-cluster / per-DID classification
> with per-DID **forbidden**, an explicit forbidden-shape list, and an
> add-a-surface checklist. They also name a dashboard at
> `driftwatch.sp00ky.net`. The doctrine is real and closely argued.

What Driftwatch lacks is *mechanical enforcement* of that doctrine, and the gap
has already produced a live divergence. There is no privacy-gate module
comparable to Weatherwatch's `publication.py`; observation health is ephemeral
process state that cannot be attributed to a historical window; and three
per-DID HTTP routes existed **in contradiction of the repository's own written
rule**, absent from its own surfaces inventory (Breakwater Stage 6–7; the
repository's 2026-07-17 codex audit had already filed this as finding #4,
"INTERSECTS RATIFIED DOCTRINE"). A Murex endpoint on Driftwatch would not be
exposing a settled boundary; it would be publishing across one that the code
does not yet hold. That is the preparatory work clause of decision B, and
Section 8 names the exact repairs.

---

## 2. Reconnaissance

### 2.1 What each instrument publishes today

| | Weatherwatch | Labelwatch | Driftwatch |
|---|---|---|---|
| public site | `weatherwatch.neutral.zone` | `labelwatch.neutral.zone` | `driftwatch.sp00ky.net` per `PUBLIC_SURFACES.md`; liveness not verified |
| serving | static files, atomic dir swap | nginx over `/var/www/labelwatch` + a `127.0.0.1` HTTP service | `127.0.0.1:8422` only |
| public JSON | `summary.json`, `history/index.json`, `history/<date>.json`, `social.json`, `findings/**` | `overview.json`, `labelers.json`, `alerts.json`, `labeler/<slug>.json`, `alert/<id>.json` | none |
| schema versioning | yes, per artifact (`weatherwatch.summary/v2`, …) | `api_version: "v0"` on `overview.json` only | n/a |
| publication doctrine | `docs/PUBLIC-ARTIFACTS.md` | `docs/architecture/PUBLIC_SURFACES.md` | `docs/architecture/PUBLIC_SURFACES.md` + publication-boundary diagram |
| privacy gate **enforced in code** | yes — `publication.py`, byte-level identity tripwire | no equivalent gate | no equivalent gate |
| bounded artifacts | yes, explicitly (`archive.RECENT_SECONDS`, `RECENT_CAP`) | no — `alerts.json` grows without bound | n/a |

Evidence: `weatherwatch/docs/PUBLIC-ARTIFACTS.md`;
`weatherwatch/src/weatherwatch/archive.py`;
`weatherwatch/src/weatherwatch/publication.py`;
`labelwatch/src/labelwatch/report.py:1804-1853`;
`labelwatch/deploy/labelwatch.nginx.conf`;
`driftwatch/deploy/docker-compose.prod.yml`.

### 2.2 The reducer boundary that produces those facts

- **Weatherwatch.** `classify()` emits only symbols from a finite 63-entry
  metric alphabet; the event never leaves the classifier. Persisted state is
  integer counts per `(run, window, metric)`, one `window_health` row per
  window, and a resume cursor. `report._summary_json`
  (`src/weatherwatch/report.py:1715`) is the single reduction that produces both
  the page and the JSON — there is no second interpretation.
- **Labelwatch.** `queryLabels` polling → append-only `label_events` → `scan.py`
  rules → `alerts`; `derive.py` produces four independent per-labeler dials.
  `frontdoor.network_weather` (`src/labelwatch/frontdoor.py:744`) is the closest
  thing to an estate-level reduced fact set, and `weather_digest.build_digest`
  (`src/labelwatch/weather_digest.py:156`) wraps it in a receipted, windowed,
  hash-sealed envelope explicitly intended for "cron / RSS / external
  consumers." That envelope is the natural Murex producer for Labelwatch.
- **Driftwatch.** Jetstream → `claim_history` → fingerprint pipeline →
  `driftmetrics.cluster_report`. The reduction unit is a *claim cluster* keyed
  by `claim_fingerprint`.

### 2.3 Time windows

- Weatherwatch: 60-second buckets closed by the stream's own clock; whole-second
  `bucket_start`; UTC-day archive partitions.
- Labelwatch: rolling relative lookbacks computed at render time (24h / 7d /
  30d) plus a per-labeler `coverage_window_minutes`. There are no closed,
  addressable windows — a "7d" figure is a query against `now`, not a sealed
  interval.
- Driftwatch: `hours`-parameterised lookback with `bin_hours` bins, also
  relative to `now`.

Only Weatherwatch currently has windows a consumer could name, re-fetch, and
get the same answer for.

### 2.4 Freshness, coverage, degradation, unknown state, source failure

All three repositories **already share a byte-identical envelope for exactly
this**: `.ops/status.schema.json`, `$id: project.ops.status/v1`.

```
weatherwatch/.ops/status.schema.json  sha256 7fd26c251876caa0…
labelwatch/.ops/status.schema.json    sha256 7fd26c251876caa0…
driftwatch/.ops/status.schema.json    sha256 7fd26c251876caa0…
```

It carries the six-state vocabulary `PRESENT / DEGRADED / UNKNOWN / STALE /
ABSENT / REFUSED`, plus `observation_present`, `observed_at`,
`valid_for_seconds`, `reason`, and `facts`. This is the single strongest piece
of demonstrated cross-instrument commonality in the estate, and it is the
vocabulary Murex should adopt rather than invent. Note that it is a *local
operator* interface carrying operational internals (SQLite freelist geometry,
volume capacity) and is **not itself publishable**; what transfers is the
vocabulary, not the document.

Beneath that shared vocabulary the underlying measures genuinely differ:

| | coverage denominator | historically attributable? |
|---|---|---|
| Weatherwatch | observed seconds ÷ span seconds | **yes** — persisted per window in `window_health` |
| Labelwatch | successful poll attempts ÷ attempts, **per labeler** | **yes** — persisted per attempt in `ingest_outcomes` |
| Driftwatch | observed throughput ÷ a *learned EWMA baseline* | **no** — ephemeral, resets on restart |

### 2.5 Provenance and methodology

Weatherwatch is the only instrument that ships methodology inside the artifact:
`summary.json` carries `claim`, `measures`, a ten-entry `does_not_measure`
list, `source_endpoint`, `collector_version`, and `notes` restating the
observer-divergence limit. Labelwatch carries a `build_signature`
(package version, schema version, git commit, config hash) but no semantic
scope statement. Driftwatch carries `fingerprint_version` and `config_hash` on
decisions, and a window fingerprint on detection envelopes.

Weatherwatch additionally owns a *registry* of source contracts —
`.ops/composition-contracts.json`, schema
`weatherwatch.composition.contracts/v1` — in which each contract declares a
`producer_id`, a `vantage_profile`, a `coverage_profile`, per-measurement
`semantic_profile`s and units, and a finite `allowed_dimensions` vocabulary.
This is the provenance mechanism Murex needs, already built.

**It already contains a `labelwatch.issuance.v1` contract with a declared
`labelwatch.reducer.issuance.v1` producer that does not exist.** Neither
Labelwatch nor Driftwatch contains any composition producer code
(verified by search). The contract registry is currently a consumer-side
declaration waiting for producers.

### 2.6 What exists internally and is deliberately not public

- **Weatherwatch:** the social edge sink — bounded actor→subject rows in a
  *separate* local database with a 24-hour horizon, off by default in code,
  never published. Aggregate episodes may be published only behind a ≥10
  distinct actor gate plus a no-single-actor-could-explain-it gate, with times
  coarsened to UTC hours. See `src/weatherwatch/social/BOUNDARIES.md`.
- **Labelwatch:** `NON_GOALS.md` names the forbidden shape explicitly —
  "`GET /poster/{did}/weather` or any equivalent per-handle behavioral forecast
  surface", closing with *"If the tables can answer dossier-shaped questions,
  the API still must not."* The existing `/v1/climate/{did}` is deliberately
  exempt as receiving-end accounting, not behavioural forecasting. Murex must
  not disturb that distinction and must not extend it.
- **Driftwatch:** `PUBLIC_SURFACES.md` classifies per-DID surfaces as forbidden
  outright and lists forbidden shapes (`GET /poster/{did}/clusters`,
  `/weather`, `/automation_score`, `/discourse_profile`), closing with the same
  load-bearing rule Labelwatch uses: *"If the tables can answer dossier-shaped
  questions, the API still must not."* The reviewed public subset therefore
  exists on paper. Breakwater found three routes that contradicted it in code
  and moved them behind the administrative boundary; see §12.

### 2.7 Historical retention

- Weatherwatch: *"a view, not a vault."* The archive is regenerated from the
  observation store each publish, so its extent is the store's extent; shrinkage
  is visible in `day_count` / `first_archived_day` rather than silent, and a
  missing day is a 404 to be reported as unavailable, never inferred as quiet.
- Labelwatch: `label_events` is append-only and never pruned; but no dated
  historical artifacts are published, so there is no published history at all.
- Driftwatch: actively pruned — events 7d, edges 14d, claims 14d
  (`retention.py:87-89`), with a separate Parquet archive protocol. Publishing
  Driftwatch history would require either new retention (forbidden by the
  brief) or exposing the archive rail (out of scope).

### 2.8 Reusable serving and serialization infrastructure

Sufficient in all cases, and no new daemon is warranted:

- Weatherwatch: `archive._dumps` already gives sorted-key, fixed-separator
  deterministic serialization with content digests, plus render → privacy gate →
  atomic directory swap. Weatherwatch deliberately has **no HTTP server**, and
  its absence is a stated reader-protection property.
- Labelwatch: `report._write_json` into the same atomically-prepared output
  directory nginx already serves with ETag and a 120s TTL for `*.json`.
- Driftwatch: has FastAPI, but nothing publishable to serve from it.

### 2.9 Does any current surface bypass the intended fact boundary?

**Yes — two, both in the direction the brief warns about.**

**(a) Labelwatch's `overview.json` is epistemically weaker than the page it
accompanies.** The HTML report renders a "Platform coverage (24h)" card
computed from `ingest_outcomes` (`report.py:2017-2036`). `overview.json`, built
at `report.py:1804`, carries `alerts_by_rule_24h`, `alerts_by_rule_7d`,
`top_labelers_7d`, `census` and heartbeats — **and no coverage field at all**. A
machine consumer reading the JSON receives counts stripped of the qualification
a human reading the page receives. This is precisely "an API consumer obtaining
a stronger claim than the instrument is entitled to publish," and it exists
already, before Murex adds anything.

**(b) Driftwatch's HTTP app exposed unauthenticated identity-bearing routes.**
*(Repaired by Breakwater — see §12. Retained as the record of the defect.)*
`GET /exposure/{did}` (`main.py:315`), `GET /strain/top` (`main.py:324`, "top
authors by event count", returning raw author identifiers) and
`GET /labels/{subject_uri}` (`main.py:332`) carry no `admin_auth` dependency,
while `/recent-decisions` and `/quarantine/recent` do. At the time of Murex this was contained
only by the loopback port binding in `deploy/docker-compose.prod.yml`. It is one
reverse-proxy line away from being a public per-identity query surface. Murex
must not be that line — and Breakwater subsequently moved all three behind the
pre-existing `admin_auth`, because a bind is defence in depth, not a substitute
for the route carrying its own boundary.

### 2.10 Is there enough semantic commonality?

For **Weatherwatch and Labelwatch**: yes, conditionally. Both reduce a stream of
public protocol activity into counted facts over bounded windows, both persist a
coverage denominator, both already speak the same six-state validity vocabulary,
and both already have a reviewed public artifact tree and an atomic publish
path. The condition is that coverage is carried *per fact* with a declared
`coverage_profile`, never as one envelope scalar.

For **Driftwatch**: not at this time — and the obstruction is custody, not
ontology. Driftwatch's aggregate shape (claims per hour, distinct clusters per
hour) would fit the envelope fine. What it lacks is a publication boundary, a
privacy gate, historically attributable coverage, and a k-anonymity threshold.
Those are buildable; they are not built.

---

## 3. The proposed common contract

### 3.1 Shape

Promote the existing bundle fact — unchanged in field set — into a published
artifact envelope:

```json
{
  "schema": "atproto-observatory.facts/v1",
  "instrument": "weatherwatch",
  "generated_at": "2026-08-28T14:05:00Z",
  "contracts": "contracts.json",
  "facts": [
    {
      "contract_id": "weatherwatch.jetstream.production.v1",
      "measurement": "atproto.public_record_writes",
      "window": { "start": "2026-08-28T13:00:00Z", "end": "2026-08-28T14:00:00Z" },
      "acquired_at": "2026-08-28T14:00:03Z",
      "state": "PRESENT",
      "coverage_fraction": 0.98,
      "value": 412233,
      "unit": "count",
      "dimensions": { "record_family": "app_bsky" }
    }
  ]
}
```

The `fact` object is byte-for-byte the existing
`weatherwatch.composition.bundle/v1` fact definition. Only the wrapper is new,
and it adds exactly three keys: `instrument`, `generated_at`, and a pointer to
the contract registry.

### 3.2 Why not `/api/v1/<instrument>/…`

The brief asked for this to be validated rather than obeyed. It does not
survive validation:

- **Weatherwatch has no HTTP server, deliberately.** Its README and deploy
  notes state that there is no application server, API, or read endpoint, and
  that "nothing about a visitor [is] observable beyond ordinary web-server
  logs." An endpoint would trade away a stated reader-protection property for
  no capability a static file lacks.
- **Labelwatch already owns `/v1/*`, with per-identity semantics.** `/health`,
  `/about`, `/claims`, `/v1/registry`, `/v1/frontdoor`, `/v1/climate/{did}`,
  `/v1/whatsonme/{did}` (`server.py:236-248`). Placing aggregate instrument
  facts in the same namespace as a per-DID lookup blurs the exact distinction
  `NON_GOALS.md` calls load-bearing.
- **Driftwatch must serve nothing publicly** until Section 8 is discharged.
- GET/HEAD JSON over ordinary HTTP is fully satisfied by a static artifact, and
  a static artifact additionally gets caching, mirroring, `curl` loops and
  request-log-only observability for free.

**Proposed surface instead** — two files per publishing instrument, beside the
artifacts each already publishes:

```
https://weatherwatch.neutral.zone/facts.json
https://weatherwatch.neutral.zone/contracts.json
https://labelwatch.neutral.zone/facts.json
https://labelwatch.neutral.zone/contracts.json
```

`contracts.json` is the already-existing
`weatherwatch.composition.contracts/v1` registry, published rather than
invented, so that every `contract_id`, `coverage_profile` and `semantic_profile`
a fact references is dereferenceable by the consumer.

### 3.3 Instrument-specific extension points

There are exactly two, and neither is an envelope field:

1. **`contract_id`** selects the vantage, coverage and semantic profiles. This
   is where instruments differ, and it differs by *reference to a registry*
   rather than by shape, so no consumer can accidentally compare a Weatherwatch
   `coverage_fraction` with a Labelwatch one without dereferencing two different
   `coverage_profile` values.
2. **`dimensions`** selects from `INSTALLED_DIMENSIONS`
   (`composition.py:74-78`) — currently `record_family`, `temporal_phase`,
   `service_state`. A contract may *select* from these vocabularies; it cannot
   invent a name or a value. New dimensions require a code and test change.

There is no `extensions` object, no free-form `facts` sub-schema per instrument,
and no per-instrument envelope key. An instrument that needs to say something
the fact shape cannot say does not get an escape hatch; it gets a contract
amendment reviewed as a code change.

### 3.4 Explicit exclusions

The surface is GET/HEAD JSON. It is not, and must not become: a query API, a
filtering DSL, GraphQL, SQL-over-HTTP, a raw firehose, webhooks, SSE/WebSocket
streaming, a client SDK, an authenticated tier, a per-user dashboard, a write
verb, or a remediation plane. It adds no retention, no daemon, and no
privileged runtime access. Adding a query dimension is a code-and-test change,
by construction.

---

## 4. Epistemic semantics preserved

| brief requirement | mechanism, already built |
|---|---|
| reduced facts, not raw events | facts carry one `value` per `measurement`; the classifier alphabet and reducers sit upstream |
| explicit observation windows | mandatory per-fact `window.start` / `window.end`, timezone-required |
| absent evidence ≠ evidence of absence | mandatory `state` from the six-state vocabulary; `ABSENT`, `UNKNOWN`, and `PRESENT`-with-`value: 0` are three different documents |
| freshness preserved | per-fact `acquired_at`, distinct from envelope `generated_at` |
| degraded / incomplete / unknown not coerced | `state` is not derivable from `value`; a `DEGRADED` fact keeps its value *and* its state |
| provenance sufficient to interpret | `contract_id` → `vantage_profile`, `coverage_profile`, `semantic_profile`, `producer_id` |
| weakest-coverage-wins composition | already implemented and tested as composition rule C2 — composite state is the weakest participating state, composite coverage the minimum participating fraction, never the union |
| no stronger claim than the instrument may publish | facts are emitted by the same reducer that renders the page; a Murex producer must never re-derive |
| no routing around the reducer | no lower-level endpoint exists to route to; the surface is a file |
| history not silently reinterpreted as complete | Weatherwatch's "view, not a vault" posture carries over verbatim; absent history is a 404 to report as unavailable, never as quiet |

---

## 5. Where the brief's illustrative envelope was wrong

The brief flagged its envelope as illustrative and asked it to be tested. Two
fields fail the test.

**Envelope-level `coverage` is not expressible for Labelwatch.** Labelwatch
coverage is `successes ÷ attempts` *per labeler* over a configured window
(`rules.py:67-94`). Every discovered labeler has independent reachability and
therefore its own coverage ratio. Any single envelope scalar is either a mean
(which hides one dead labeler among many healthy ones), a minimum (which
reports the estate as broken whenever any one endpoint is down), or a
poll-weighted ratio (which silently weights by polling frequency rather than by
anything a reader cares about). All three are fabrications. Per-fact
`coverage_fraction` avoids the choice entirely: a labeler-scoped fact carries
its own labeler's coverage, and a consumer that wants an estate figure must
compose one and own that composition.

**Envelope-level `window` forces facts into a false shared interval.** A
Weatherwatch document reasonably carries minute-bucket facts and 24-hour
rate facts together; a Labelwatch document carries 24h and 7d facts together.
One envelope window would either lie about the narrow facts or lie about the
wide ones. Weatherwatch already models this correctly: `summary.json`'s
`interval` block describes the *observed extent* and explicitly does **not**
describe the retained tail, precisely so a consumer learns the true extent
without mistaking it for the windows' bounds.

**A bare `coverage_fraction` without its profile would also be a lie.** The
three instruments' coverage denominators are time, poll attempts, and a learned
baseline respectively. The number is only interpretable against its
`coverage_profile`. This is why `contracts.json` must be published alongside
`facts.json` and not treated as optional documentation.

---

## 6. Privacy and reconstructability analysis

### 6.1 The boundary already refuses the dangerous shapes

`composition.py` enforces, at the reduction boundary, that dimension names may
not contain any of `actor, account, did, handle, host, hostname, identity, key,
person, pseudonym, repo, subject, token, uri, user, hash`, and that dimension
values may not match a DID, an `at://` URI, an `https?://` URL, a `bafy…` CID,
an `a:<12 hex>` actor token, or **any bare hexadecimal run of 16 or more
characters** — "opaque stable hashes are not a privacy escape hatch"
(`composition.py:55-73`). Refusals are non-echoing: the rejected value never
appears in the refusal.

Adversarial probes run against the live code:

| probe | outcome |
|---|---|
| real Driftwatch fingerprint `24686214e84bd924` as a dimension value | `UNBOUNDED_DIMENSION` (leading digit fails the token pattern) |
| token-legal 16-hex fingerprint `ae4bd92424686214` | `IDENTITY_SHAPED_VALUE` (opaque-stable-hash rule) |
| `did:plc:abc123` as a value | refused |
| `alice.bsky.social` as a value | refused |
| dimension *names* `fingerprint_hash`, `actor_cohort`, `pds_host` | `IDENTITY_DIMENSION` |
| enumerated token `app_bsky` | admitted |
| refusal echoes the rejected value | **no** |

Both rules fire independently on the fingerprint case, which is defense in
depth rather than redundancy.

### 6.2 The Driftwatch counterexample

`claim_fingerprint` is `sha256(normalized_claim_text)`, truncated to 16 hex
characters (`claims.py:313`, `claims.py:353`). Verified empirically: the
function is pure, deterministic, unsalted, carries no secret, and is stable
across surface mutation — two differently-punctuated, differently-capitalised,
emoji-bearing renderings of the same sentence produce the identical
fingerprint.

That combination is exactly what makes it unpublishable, and the reason is not
a weakness in SHA-256. **SHA-256 is not invertible, and nothing here claims it
is.** The hazard is *candidate enumeration*: the normaliser is open source, the
corpus it addresses — public Bluesky posts — is public and enumerable, and the
function is unsalted and deterministic. An adversary does not attack the hash;
they normalise and hash candidate posts they already have and compare digests.
The published fingerprint is therefore practically re-identifiable. It is not a
pseudonym for a claim; it is a content address of specific posts, and by
extension of the accounts that made them.

It gets worse in combination. Cluster entries carry `latest_authors`, and the
detector flags `single_author_heavy` when `latest_authors <= 1 and total_posts
>= 10` (`driftmetrics.py:437-439`). A published cluster fact with
`single_author_heavy: true` is a practically re-identifiable pointer to one
identifiable account, attached to an automation-shaped label. That is simultaneously an
identity disclosure and the "accusation-shaped output" Driftwatch's own
`CLAUDE.md` forbids.

**Conclusion: `claim_fingerprint` must never appear in a Murex fact, in any
field, at any truncation.** Three corollaries, stated separately because they
are separate claims:

- **Hashing does not make the fact safely anonymous.** The digest stands in for
  the text; against an enumerable public corpus that is a distinction without a
  difference.
- **Truncation alone does not solve candidate enumeration.** A shorter digest
  raises the collision rate, which adds noise but does not remove the matching
  capability; it is not an anonymity mechanism.
- **Salting changes linkability, not publishability.** A rotating salt would
  break cross-window comparison, which is the only reason to publish a cluster
  key at all; a fixed salt would remain enumerable to anyone who learns it, and
  in either case a *single-author* semantic fact still describes one account.
  Salting is therefore not a repair for this surface.

The correct publication decision at this granularity may simply be refusal.
Designing an anonymisation mechanism is explicitly out of scope.

Driftwatch aggregate *counts* — claims observed per hour, distinct clusters per
hour, cluster-size distribution above a k-threshold — carry no such property and
remain a viable future contract.

### 6.3 Low-cardinality and cohort reconstruction

For Labelwatch, the risk is not the labelers — they are declared public
governance infrastructure whose DIDs are already deliberately published in
`labelers.json`, and whose behaviour is the instrument's subject. The risk is
the *labeled*. `derived_author_day` and `derived_author_labeler_day` are
author-keyed rollups; `boundary_targets` holds per-target composition
snapshots. None of these may enter a Murex fact, and the enumerated-dimension
rule already makes an `author` or `target` dimension structurally unavailable
rather than merely discouraged.

The residual risk that the dimension rule does *not* cover is
**low-cardinality aggregates**: a labeler-scoped issuance count of `1` over a
one-hour window discloses that a specific labeler acted once, and combined with
the public `queryLabels` endpoint that is a near-pointer to which action. This
is the one genuinely new exposure Murex would create, and it is why Section 8
lists a minimum-count suppression rule as a Labelwatch prerequisite rather than
an optimisation. Weatherwatch's PLC reducer already establishes the estate
precedent for the mechanism (0–9 collapse to a single `UNKNOWN`) *and* for its
honest qualification — per-fact suppression is claimed; compositional
non-disclosure across multiple facts or revisions is explicitly **not**
claimed. Murex inherits both the mechanism and the refusal to overclaim it.

### 6.4 What the surface still cannot promise

Disclosure resistance, not anonymity. Per-fact suppression, not compositional
non-disclosure across facts, across windows, or across successive publications
of the same window. A consumer archiving `facts.json` every five minutes
accumulates a revision history the instruments do not model. Naming this is a
prerequisite deliverable, not a footnote.

---

## 7. Decision: B — SHARED-SURFACE-DESIGN-ONLY

**Why not A.** A requires that *the repositories* already have fact boundaries
clean enough to implement without semantic distortion or substantial surgery.
Weatherwatch does. Labelwatch does not — its published JSON omits the coverage
its own page displays, and its estate-level reducer manufactures `calm` from a
total outage (Section 8.2). Driftwatch does not have a publication boundary at
all. Two of three fail, so A is false.

Implementing a Weatherwatch-only slice was considered and rejected on the
brief's own terms. Weatherwatch already publishes these facts; a `facts.json`
alongside would be a second serialization of the same reduction, and its value
is entirely in being the *shared* format. Shipping it as the sole producer would
freeze the contract around the one instrument that constrains it least — the
exact failure the reconnaissance-first instruction exists to prevent — while
adding a published artifact whose stated purpose no second producer yet
fulfils. That is speculative infrastructure for a hypothetical consumer.

**Why not C.** C requires the instruments to be materially different enough that
a shared API would create false commonality. Tested directly, they are not: all
three already ship a byte-identical `project.ops.status/v1` envelope and share
its six-state validity vocabulary, and Weatherwatch's fact shape accommodates
Labelwatch's measurements without distortion *provided* coverage is per-fact and
profile-qualified. The commonality is real; it is the *proposed envelope's*
placement of `coverage` and `window` that was false, and correcting it is a
design fix, not a disproof. Driftwatch's exclusion is a custody gap with named
repairs, not an ontological mismatch.

**Therefore B.** The contract is specified above. The prerequisites are below.
No code was written.

---

## 8. Exact prerequisite repairs

Ordered by what blocks what. Each is a repository-owned decision; none is
authorised by this document.

### 8.1 Weatherwatch — smallest set, all additive

1. **Promote the bundle fact to an estate-owned schema id.** `.ops/composition-bundle.schema.json` is `urn:weatherwatch:schema:composition-bundle:v1`; a published cross-instrument contract cannot stay in a single instrument's URN namespace. Field set unchanged; identifier and ownership change.
2. **Add a `facts` producer** emitting from `_summary_json`'s existing series map, not re-deriving. A Murex producer that recomputes is a second interpretation and is disqualified by the brief.
3. **Publish `contracts.json`** — the existing registry, unmodified, added to the rendered tree.
4. **Extend `publication.REQUIRED_ARTIFACTS`** to include both files so the existing identity tripwire and structural gate cover them. The tripwire needs no new patterns; it already catches every prohibited shape.

### 8.2 Labelwatch — two are correctness repairs, not Murex features

1. **`network_weather` must not report `calm` during an ingest outage.** Verified empirically against `frontdoor.network_weather` with a synthetic estate of twelve labelers and a total 24-hour outage — **0 of 576 poll attempts successful** — the function returns `signals: ["calm"]`, `attribution: "no triggers crossed"`, and its return keys are `attribution, computed_at, emitting_this_week, events_7d_total, signals, total_labelers, unreachable` — no coverage, staleness, or freshness field of any kind. Signals derive from alert counts, and a dead ingest produces no alerts. This converts absence of evidence into evidence of absence, and it is the single hardest blocker: `network_weather` feeds `weather_digest`, the homepage strip, and the report, so a Murex fact built on it would inherit the defect. **This is a live defect in the existing instrument, independent of Murex.**
2. **Carry coverage into `overview.json`.** The data exists in `ingest_outcomes` and is already rendered as an HTML card (`report.py:2017-2036`). Until it reaches the JSON, the machine surface makes a stronger claim than the page.
3. **Define closed, addressable windows.** Current 24h/7d/30d lookbacks are relative to render-time `now`; two fetches a minute apart describe different intervals under identical labels. Murex facts need windows a consumer can name and re-fetch.
4. **Add minimum-count suppression** for labeler-scoped facts, following the PLC reducer's precedent and inheriting its explicit refusal to claim compositional non-disclosure (Section 6.3).
5. **Bound `alerts.json`**, or exclude it from anything Murex references. It grows without limit; Weatherwatch already solved the same problem and the recent-plus-dated-archive pattern transfers directly.
6. **Version the artifacts properly.** `api_version: "v0"` on `overview.json` alone is not a per-artifact schema contract.

### 8.3 Driftwatch — the work is a publication boundary, not an endpoint

1. **Decide, and record, what Driftwatch is entitled to publish at all.** No such determination exists. This is a product decision that precedes every item below.
2. **Build a privacy gate.** There is no analogue to `weatherwatch/publication.py` anywhere in the repository. Nothing should be published from Driftwatch before one exists and is tested against a fixture corpus.
3. **Rule `claim_fingerprint` permanently out of the public surface** (Section 6.2), including salted and truncated derivatives.
4. **Persist per-window observation health.** `platform_health` is documented as ephemeral runtime state that resets on restart, and `cluster_report` attaches the *current* snapshot to an `hours`-long *historical* window (`driftmetrics.py:452-466`). Every fact would carry coverage describing the wrong time. Weatherwatch's `window_health` table is the working model.
5. **Do not publish `coverage_pct` as `coverage_fraction` under any profile.** It is a ratio against a learned EWMA baseline, not a fraction of a known denominator — the same "no canonical denominator" problem Weatherwatch already refuses for network totals. It is a gate input, not a coverage measure.
6. **Fix the unauthenticated identity-bearing routes** (Section 2.9b) — `/exposure/{did}`, `/strain/top`, `/labels/{subject_uri}`. This is worth doing on its own merits regardless of Murex, since only a loopback binding currently contains them.
7. **Accept that history is out of scope.** Retention is 7–14 days and the brief forbids broadening it to make an API attractive.

---

## 9. Verification performed

No production code, configuration, or deployment artifact was modified in any
repository. Verification was reconnaissance plus focused adversarial probing.

**Repository-native suites**, run as found:

| repository | result | notes |
|---|---|---|
| Weatherwatch | 115 passed, 1 failed | sole failure `test_report.py::test_collector_unit_uses_the_repo_endpoint`, `ModuleNotFoundError: websockets` — environment dependency gap, not a defect |
| Labelwatch | 773 passed, 7 failed | 4 further modules uncollectable (`ModuleNotFoundError: atproto`). 6 of the 7 failures reproduce on a clean `git archive` export of `HEAD`, so they are pre-existing repository failures, not worktree artifacts; the 7th is in the untracked in-flight ops-visibility work |
| Driftwatch | 422 passed, 13 failed, 122 skipped | 1 module uncollectable (`ModuleNotFoundError: pyarrow`). 12 failures are in `tests/test_retention_archive_single_protocol.py`, an **untracked** in-flight test file; 1 is a missing-dependency import error |

All failures were present before this campaign and none is attributable to it.

**Focused campaign verification:**

1. **Driftwatch fingerprint re-identifiability** — called `claims.fingerprint_text` directly. Confirmed pure, deterministic, unsalted, and stable across whitespace/case/punctuation/emoji mutation. This establishes candidate-enumeration exposure, not any weakness in SHA-256. Basis for Section 6.2.
2. **Labelwatch absent-evidence defect** — constructed a synthetic estate (12 labelers, 576 failed poll attempts, 0 successes over 24h) against a real initialised schema and called `frontdoor.network_weather`. Returned `calm` / "no triggers crossed" with no coverage or staleness field. Basis for Section 8.2.1.
3. **Reduction-boundary adversarial probes** — six probes against `composition._dimension_token`, including a real Driftwatch fingerprint. All identity-shaped inputs refused, enumerated token admitted, no refusal echoed its input. Basis for Section 6.1.
4. **Cross-repository envelope identity** — `sha256` comparison of `.ops/status.schema.json` across all three repositories: byte-identical. Basis for Section 2.4.
5. **Producer-absence check** — searched all three repositories for composition-bundle producers. `labelwatch.issuance.v1` is declared in Weatherwatch's registry with a `producer_id` that exists nowhere. Basis for Section 2.5.

Not performed, because no implementation occurred: the thirteen-item test
matrix in the brief. It applies to a producer, and no producer was written.
Items 9 (absence of prohibited identity fields) and 10 (absence of raw
collector payloads) were partially exercised by probe 3 against the boundary
those tests would protect.

---

## 10. Limitations

- **Deployment state was only partially determinable** (revisited by
  Breakwater Stage 5). Production is a remote single box, not the workstation
  these repositories are checked out on: no instrument process, container,
  systemd unit or listening socket for any of the three exists locally.
  Committed evidence establishes that Caddy is the shared reverse proxy on that
  box (7 sites), that it proxies Labelwatch's `/v1/frontdoor` to
  `localhost:8423`, and that Driftwatch's container publishes only
  `127.0.0.1:8422`. **Whether Caddy also fronts 8422 could not be determined
  from the repositories**: the Caddyfile lives at an operator path on the
  server and `DEPLOY_HOST` is deliberately not recorded in any checkout.
  Section 2.9b's severity therefore remains partly unresolved, which is one
  reason Breakwater hardened the routes rather than relying on the bind.
- **Labelwatch's live estate was not measured.** The low-cardinality analysis in
  Section 6.3 reasons about labeler-scoped counts in principle; the actual
  distribution of per-labeler per-window issuance counts on the live database
  was not sampled, and a suppression threshold cannot be chosen without it.
- **The `atproto`-dependent Labelwatch modules were not exercised**
  (`posting.py`, `findings.py`, `publication.py`, `discovery_stream.py`).
  `publication.py` was read; it governs Bluesky posting readiness and is
  unrelated to the artifact publication boundary despite the shared filename.
- **No consumer was interviewed.** The brief forbids speculative infrastructure
  for hypothetical consumers; it follows that this contract is unvalidated
  against a real one. The prerequisites should not be executed until at least
  one intended consumer's needs are known.
- **Weatherwatch's own composition rules were not re-verified**; C0–C2 are taken
  as implemented per `docs/TEMPORAL-COMPOSITION.md` and the passing
  `tests/test_composition.py` collection, which could not be run in this
  environment owing to the `websockets` gap.

---

## 11. Result

**Codename:** Murex
**Canonical slug:** public-readonly-instrument-fact-api
**Track:** public-observation-surface
**Implementation decision:** B — SHARED-SURFACE-DESIGN-ONLY
**Result classification:** shared envelope sustained for Weatherwatch and
Labelwatch, with the brief's proposed envelope corrected; disproved as a
three-instrument surface at this time on custody grounds; prerequisites named
per instrument.
**Public surface created:** none.
**Code changed:** none.

---

## 12. Breakwater outcomes (follow-up campaign, 2026-08-28)

Campaign: **Breakwater** / `observation-adequacy-and-public-boundary-hardening`
Track: public-observation-surface

Breakwater discharged the Murex prerequisites that were defects in their own
right, and corrected this record. **It did not implement the Murex surface, and
the Murex decision is unchanged: B — SHARED-SURFACE-DESIGN-ONLY.**

### 12.1 What Breakwater changed

| prerequisite | status |
|---|---|
| §8.2.1 Labelwatch `calm` under total ingest outage | **repaired** |
| §8.2.2 Labelwatch machine/human epistemic parity | **repaired** |
| §2.9b Driftwatch unauthenticated identity-bearing routes | **repaired** |
| §6.2 fingerprint privacy wording | **corrected** — see §6.2 |
| §8.1 Weatherwatch producer work | **not started** — correctly, no consumer |
| §8.2.3–6 Labelwatch windows, suppression, bounding, versioning | **not started** |
| §8.3 Driftwatch publication-boundary enforcement | **not started** |

**Labelwatch adequacy.** The coverage watermark was wired to *suppress*
positive findings and nothing was wired to *qualify* the negative one, so
degraded observation manufactured the calm reading it should have blocked.
`frontdoor.observation_adequacy` now states standing from the same facts
(`ingest_outcomes`) and the same threshold (`Config.coverage_threshold`) the
detection rules already use — no new threshold was introduced — and `calm` is
emitted only with standing. Positive signals are unaffected: an observed spike
was observed.

**Labelwatch parity.** `overview.json` now carries `observation` and
`network_weather`, and the report page's weather verdict is computed once and
shared rather than derived twice. Coverage is reported as per-labeler counts
against a named denominator; no estate-wide scalar was fabricated, per §5.

**Driftwatch routes.** `/exposure/{did}`, `/strain/top` and
`/labels/{subject_uri}` now require the pre-existing `admin_auth` dependency.
No new authentication system was introduced. A bounded audit of the remaining
routes found the health and metrics endpoints carry no per-account material.
A constellation-wide search found **no consumer of any kind** for the three
routes.

### 12.2 What Breakwater deliberately did not change

Driftwatch **remains excluded from the Murex public fact surface.** Route
hardening removed a contradiction between Driftwatch's code and its own written
doctrine; it did not create the missing enforcement machinery, persist
per-window observation health, or answer what Driftwatch may publish. Every
item in §8.3 stands. Hardening an internal diagnostic boundary is not the same
act as earning a public one.

The architectural rule of §3 also stands unchanged:

> Weatherwatch and Labelwatch should reuse the existing reduced-fact
> interchange shape rather than create a second drifting public API contract.

Breakwater implemented no query API, filtering DSL, GraphQL, SQL-over-HTTP,
firehose, webhook, streaming endpoint, SDK, auth tier, user dashboard,
write/remediation plane, new daemon, or shared semantic runtime. A real
consumer still gates implementation, and none has been identified.
