# Boundaries

This package retains identity. The package it lives inside does not. That
sentence is the whole reason this file exists.

## What changed, and what did not

`weatherwatch` counts aggregate ATProto event rates and keeps no people in its
weather lane. Its public artifacts contain no account identifiers, graph, or
post text. That direct-identifier guarantee is structural, not promised —
`classify()` emits values from a finite ~90-entry metric alphabet, so a DID
cannot appear in its output, and `tests/test_classify_privacy.py` checks the
containment on every fixture. It is not an anonymity claim; the separate
disclosure controls below address joins against public activity.

None of that changed.

What was added is a **second sink on the same parsed message**:

```
                       Jetstream (one endpoint, one socket)
                                   |
                        collector._handle_raw(raw)
                                   |
             +---------------------+---------------------+
             |                                           |
     classify(msg) -> counters                   social sink -> edges
     data/weatherwatch.sqlite                    data/social.sqlite
     keeps no people                             keeps actor->subject edges
     ON by default                               OFF by default
     published                                   local only
```

Two lanes, two files, two retention postures, one observation. The weather
lane cannot see the social lane; the social lane never writes to the weather
database (`test_social_lane_never_writes_to_the_weather_database`).

**The code default remains off.** The current deployment explicitly enables a
narrow `block,listitem` sink with 24-hour retention, as recorded below. The
weather lane remains structurally separate whether the second sink is on or
off.

## Why the edge lane has to exist at all

Concentration, overlap and synchronisation are statements about *who acted on
whom*. A counter has discarded that before it is stored. There is no effort
level at which the aggregate tier can answer them, and approximating them from
counts would produce a fabrication with a plausible shape. So either the
questions go unanswered or the edges are retained. They are retained, in a
separate file, off by default, with a horizon.

## The shape this package refuses

The rejected product is the per-account dossier — the thing that takes a
handle and returns a portrait. This repository enforces the boundary directly:
no arbitrary-handle lookup, leaderboards, named edge exports, shareable account
pages, timeline-causality UI, or per-account scoring. Weather is aggregate
system state, not individual behavioural telemetry.

Note what the objection is *not*. It is not "the output might be inferential."
"31% of this account's blocks went to these twelve repos" is perfectly
descriptive and still a dossier. The danger accrues at the **join**, and a
descriptive join is still a join.

So the subject of every detection here is an **episode** — a bounded stretch of
observed activity — and never an account:

| forbidden | why it is not merely absent |
|---|---|
| `SubjectRef("did", ...)` | `test_every_subject_is_an_episode` |
| any DID / AT-URI / handle on an envelope | `test_no_envelope_ever_serialises_an_identifier` |
| a per-actor rollup table | `test_edge_store_has_no_dossier_table` (schema is an allowlist) |
| post text, handles, profiles in storage | `test_no_table_has_a_content_or_profile_column` |
| arbitrary-handle lookup, search, ranking | `test_report_has_no_lookup_or_ranking_surface` |
| type strings naming a mechanism | `test_no_type_string_names_a_mechanism` |

Actor tokens on a finding are salted per store. That is **not** anonymisation —
the DID space is public and enumerable, so an unsalted hash would be
pseudonymity theatre. The salt keeps identity labels off the envelope surface
and keeps two stores from being joined by token. Nothing more is claimed.

## Public disclosure resistance

An identity-free field list is necessary and insufficient. ATProto activity is
publicly observable; an exact row such as “two block events in these two
minutes” may identify the accounts by trivial reconstruction even if the row
contains no DID. Exact timing, low cardinality, lifecycle adjacency, repeated
rare signatures, and combinations of exact counts/rates/receipts can recreate
an identifying join.

The public projection therefore applies all of these rules, in order:

1. Only `aggregate_rate_episode` is considered. Edge and lifecycle findings
   remain local regardless of their field shape.
2. The already-local edge store must independently witness at least **10
   distinct actor DIDs** for the same collection, operation, and exact episode
   interval. Event count is not a substitute: one account can emit many events.
3. Missing edge data, expired retention, an unsupported metric, malformed
   envelope data, or fewer than 10 observed actors suppresses the episode.
4. Eligible timestamps are rounded outward to one-hour UTC boundaries.
5. Exact counts, rates, z-scores, temporal shape, window counts, evidence and
   receipt hashes, configuration hashes, and stable episode/detection IDs are
   omitted.
6. Repeated episodes that reduce to the same coarse type/direction/band/hour
   signature collapse to one public row. Suppressed and collapsed counts are
   not published, because enumerating them would partly undo suppression.

The 10-actor floor and one-hour interval are **provisional disclosure controls**,
not statistically derived privacy parameters. They rule out the adversarial
one-account, two-account, and tiny-cohort cases and make timing joins harder;
they do not establish anonymity. Tests in `tests/social/test_surface.py` carry
the adversarial corpus and require every missing-evidence path to fail closed.

## Vocabulary discipline

`coordinated`, `brigade`, `harassment`, `attack`, `mob`, `bad actor` appear in
no type string, and a test enforces it. Those are claims about intent; this
instrument observes records. High target-set overlap is consistent with a
shared blocklist, with independent reaction to a common target, and with
coincidence at low n — so every overlap finding carries `target_prevalence`,
the base rate that makes the boring explanation visible next to the number.

`severity` is a magnitude band, not a verdict. `critical` means the ground
moved a long way. The bands are provisional and uncalibrated; the underlying
ratio and z are on every finding so a reader can ignore them.

Deactivation findings are named `deactivation_after_inbound_excess`, not
"deactivation following pressure". *Pressure* is an interpretation of the
inbound count. The count is what was observed. The detector also emits the
negative case (`deactivation_without_inbound_excess`) by default, because a
detector that only ever produces confirming instances turns a co-occurrence
rate into a mechanism.

## Retention

As observed at one endpoint: likes ~216/s, reposts ~34/s, follows ~26/s,
blocks ~5/s. Retaining every tracked edge is ~24M rows/day. Hence:

* off unless `--social-edges` is passed;
* `--social-collections` defaults to `block,follow,listitem`, not everything;
* `--social-retention` defaults to `3d` and prunes on flush.

Deletes are retained with an unknown subject. A withdrawal is an event, and an
instrument that only records creation cannot see anyone leave.

`record_created_at` is producer-controlled — M0 observed year-2999 values — so
it is stored verbatim as a claim by the emitting repo and is never used to
order or window anything. All ordering uses Jetstream `time_us`, the same
clock the weather lane windows on.

## Ratified 2026-08-22 — activation and publication

Two items were on the list below and have now been done, on operator
instruction ("enable the opt-in social observation sink in the deployed/local
collector path and expose the resulting episode observations in the existing
web UI"). Recording what was authorised, and what was *not*:

**1. The sink is enabled on the deployed collector.**
The operator-managed configuration sets `WW_SOCIAL_EDGES=1`,
`WW_SOCIAL_COLLECTIONS=block,listitem`, and `WW_SOCIAL_RETENTION=24h`.

`follow` was deliberately left out despite being in the default set: it runs
~26/s, roughly 75% of the candidate volume, and follow bursts are already
visible at the aggregate tier. Narrowing custody is always in scope; widening
it is a fresh decision.

Removing the drop-in and restarting returns the collector to counters-only.
The receipt is written either way.

**2. Disclosure-qualified aggregate episode periods are published.**
`https://weatherwatch.neutral.zone/` grew section E, and
`social.json` sits beside `summary.json` as its read side.

What that does *not* authorise, and what is still true:

* **No HTTP service.** The published artifact is a rendered directory, exactly
  as before. There is still no server, no API endpoint and no query surface in
  this codebase.
* **The edge tier is not published.** `PUBLIC_DETECTORS` admits
  `aggregate_rate_episode` and nothing else. The aggregate detector reads
  identity-free buckets; the local edge store is consulted only for the
  cardinality gate above. No actor value crosses the projection boundary.
  Concentration, overlap, synchronisation and lifecycle findings stay local.
* **The local seismogram is still local.** `social/report.py` renders every
  tier and is not published by anything.

Guards added with the publication, not assumed:
`assert_identity_free()` at the projection boundary and again in `api.build()`;
`deploy/publish.sh`'s privacy gate extended with an `a:[0-9a-f]{12}` arm for
salted actor tokens — an arm that should never fire, which is why it is there.
The disclosure rules above are separate from those identifier-shape tripwires.

## What would need re-justification

Any of these is a different decision class, not an increment:

1. Widening `WW_SOCIAL_COLLECTIONS`, or lengthening the retention horizon.
2. Publishing any detector beyond `PUBLIC_DETECTORS`, or serving any of this
   over HTTP as a service rather than a rendered file.
3. A subject type other than `episode`.
4. Any table keyed by actor alone.
5. Unsalting, or exporting, actor tokens.
6. A type string that names a mechanism or an intent.

Update this file before shipping any of them, not after.
