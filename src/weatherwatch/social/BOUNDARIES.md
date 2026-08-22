# Boundaries

This package retains identity. The package it lives inside does not. That
sentence is the whole reason this file exists.

## What changed, and what did not

`weatherwatch` counts aggregate ATProto event rates and keeps no people. Its
published page states that plainly: *"Nothing here identifies anyone,
reconstructs a social graph, reads any post, or detects a dispute."* That
guarantee is structural, not promised — `classify()` emits values from a
finite ~90-entry metric alphabet, so a DID cannot appear in its output, and
`tests/test_classify_privacy.py` checks the containment on every fixture.

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

**The deployed collector does not run the sink.** `--social-edges` is opt-in
and absent from every default configuration. The published page's guarantee is
a property of what runs, not only of what is written in a source file.

## Why the edge lane has to exist at all

Concentration, overlap and synchronisation are statements about *who acted on
whom*. A counter has discarded that before it is stored. There is no effort
level at which the aggregate tier can answer them, and approximating them from
counts would produce a fabrication with a plausible shape. So either the
questions go unanswered or the edges are retained. They are retained, in a
separate file, off by default, with a horizon.

## The shape this package refuses

The rejected product is the per-account dossier — the thing that takes a
handle and returns a portrait. Three ratified documents in this workspace
already forbid it, and none of them is overridden here:

* **`labelwatch/NON_GOALS.md`** — *"No poster dossiers. No per-DID behavioral
  forecast, volatility score, risk class... If the tables can answer
  dossier-shaped questions, the API still must not."*
* **`nebgraph/ETHICS.md`** — *"Boundary observed ≠ motive inferred. Public data
  ≠ harmless recomposition. The join is the ethical event."* Plus, by name: no
  hosted arbitrary-handle lookup, no leaderboards, no shareable result pages,
  no export of named edge lists, no timeline-causality UI.
* **`weatherwatch`'s own posture** — weather is aggregate system state, not
  individual behavioural telemetry.

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
`/etc/systemd/system/weatherwatch-collector.service.d/social.conf` sets
`WW_SOCIAL_EDGES=1`, `WW_SOCIAL_COLLECTIONS=block,listitem`,
`WW_SOCIAL_RETENTION=24h`, writing `/var/lib/weatherwatch/social.sqlite`.

`follow` was deliberately left out despite being in the default set: it runs
~26/s, roughly 75% of the candidate volume, follow bursts are already visible
at the aggregate tier, and the host is at 87% disk. Narrowing custody is
always in scope; widening it is a fresh decision.

Removing the drop-in and restarting returns the collector to counters-only.
The receipt is written either way.

**2. Episodes are published — the aggregate tier only.**
`https://labelwatch.neutral.zone/weatherwatch` grew section E, and
`social.json` sits beside `summary.json` as its read side.

What that does *not* authorise, and what is still true:

* **No HTTP service.** The published artifact is a rendered directory, exactly
  as before. There is still no server, no API endpoint and no query surface in
  this codebase.
* **The edge tier is not published.** `PUBLIC_DETECTORS` admits
  `aggregate_rate_episode` and nothing else. Aggregate episodes are computed
  from `bucket` counts, which never contained an actor or a target — so the
  published section is identity-free by *lineage*, not by redaction.
  Concentration, overlap, synchronisation and lifecycle findings stay on the
  collecting host.
* **The local seismogram is still local.** `social/report.py` renders every
  tier and is not published by anything.

Guards added with the publication, not assumed:
`assert_identity_free()` at the projection boundary and again in `api.build()`;
`deploy/publish.sh`'s privacy gate extended with an `a:[0-9a-f]{12}` arm for
salted actor tokens — an arm that should never fire, which is why it is there.

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
