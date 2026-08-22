# social — an episode seismograph over the weatherwatch observation layer

Not a second observatory. No second Jetstream consumer, no second event
schema, no second detection object. It adds sensors and projections to the
observation layer that already exists.

```
Jetstream -> weatherwatch collector -> +-- classify() -> counters -> weather views
                                       |
                                       +-- social sink -> edges -> sensors -> DetectionEnvelope -> seismogram
```

**The subject of analysis is the episode, not the account.** Read
[`BOUNDARIES.md`](BOUNDARIES.md) before adding anything.

## Two tiers

| | aggregate | edge / lifecycle |
|---|---|---|
| reads | the buckets weatherwatch already persists | the opt-in edge store |
| new retention | **none** | actor -> subject edges |
| answers | did a rate depart from its own baseline, when did it start, peak and end | concentration, overlap, synchronisation, lifecycle co-occurrence |
| available | immediately, over all existing history | only from the moment custody starts |

The split is not a phasing decision. `classify()` emits from a finite metric
alphabet, so the aggregate tier structurally cannot see actors or targets —
concentration and overlap are unavailable to it at any effort.

## Deployed

Live since 2026-08-22 on the weatherwatch host:

| | |
|---|---|
| edge custody | **ON** — `block,listitem`, 24h horizon, `/var/lib/weatherwatch/social.sqlite` |
| detection | `weatherwatch-social-detect.timer`, hourly, `--last 24h` (~1.8s CPU) |
| published section | `https://weatherwatch.neutral.zone/` § E |
| read side | `…/weatherwatch/social.json` (`weatherwatch.social/v1`) |
| published tier | aggregate only — see [`BOUNDARIES.md`](BOUNDARIES.md) |

Activation is environment-driven and off unless explicitly set. Both states
leave a receipt: `meta.social_sink_receipt` in the weather database and
`social_sink_receipt.json` beside it, written on every run. The published page
renders the receipt, so the page states its own retention posture rather than
asking a reader to trust one.

```
WW_SOCIAL_EDGES=1                   # must be affirmative; ambiguous values are refused
WW_SOCIAL_DB=/var/lib/…/social.sqlite
WW_SOCIAL_COLLECTIONS=block,listitem
WW_SOCIAL_RETENTION=24h
WW_SOCIAL_WINDOW=48h                # how far back the published section reaches
```

## Read model

```
episode store  ->  projection (audience-gated)  ->  social.json  ->  section E
```

The renderer is handed a `SocialProjection` and no connection, so it cannot
reach past the read model into a detector table. `EpisodeView` is a whitelist
built field by field, so a detector that grows a new `explain` key cannot leak
it onto the page by passthrough.

Re-detection is idempotent **at the read model, not in storage**. Re-running a
pass over a shifted range re-observes episodes already recorded; those are
genuinely distinct detections (different scope, different coverage, different
`window_fingerprint`, so a different `det_id`) and every one is kept as the
audit trail. `episode_id` derives from the evidence segment alone and is
stable across all of them, so the projection collapses to one row per episode
and reports `n_detections` / `n_superseded` alongside. Measured on the
deployed store: a second pass turned 1,885 rows into 2,242 while adding 9
actual episodes.

## Quick start

Detection over history you already have — no custody, no new retention:

```bash
weatherwatch social detect --tiers aggregate --last 24h
weatherwatch social episodes --limit 20
weatherwatch social report --output out/seismogram
```

Turning on edge custody (changes this process's retention posture):

```bash
weatherwatch collect --duration 6h \
    --social-edges \
    --social-collections block,follow,listitem \
    --social-retention 3d

weatherwatch social custody                       # sink health and volume
weatherwatch social detect --tiers edge,lifecycle --last 6h
```

## Episodes

Aggregate tier, per metric: `block_burst` / `unblock_burst`, `like_storm`,
`repost_storm`, `follow_burst` / `unfollow_burst`, `listadd_burst`,
`delete_storm`, `account_inactive_burst` — each with a matching `*_lull`,
because withdrawal is an event and an instrument that only sees surges is
blind to a network going quiet.

Edge tier: `actor_concentration`, `target_concentration`, `cohort_overlap`,
`temporal_synchronisation`.

Lifecycle: `deactivation_after_inbound_excess` and its negative case
`deactivation_without_inbound_excess`.

Episodes of different types are **never merged**. Overlapping intervals are
reported separately by `co_occurrence()`, with no score and no asserted
relationship — merging them would be the first step in inventing a narrative
the records do not contain.

## Magnitude

Every detector reports `score` as **log2 of a ratio against its own null**, so
one unit is one doubling. Two questions stay separate:

* *Is this an episode?* — the detector's z gate.
* *How big was it?* — the ratio.

This was forced by real data. On the local weather database, a five-minute
`block.create` departure of 3.5x baseline scored z = 83, because a
median-absolute-deviation baseline over a smooth, strongly autocorrelated
minute series has almost no scale. Reporting z as magnitude put 17 of 41
episodes in the top band. On the ratio scale the same 41 episodes spread
across info / low / med / high, and a 1.03x like-rate departure reads as
`info` instead of `critical`.

The bands are provisional and uncalibrated. `explain` carries the ratio and
the z on every finding so you can ignore them.

## Baselines

The default baseline is **median / MAD**, not mean / stddev. A trailing mean is
contaminated by the excursion it just measured: the burst enters the baseline,
the baseline rises, and a second burst minutes later scores below threshold and
disappears. `test_mean_baseline_is_contaminated_by_the_first_burst` pins this —
the mean path finds one of two bursts, the median path finds both. The mean
path is kept (`baseline="mean"`) because reproducing the weather lane's own
numbers is worth more than tidiness.

Everything else about conditioning is inherited from weatherwatch's read layer
and not reimplemented: unobserved windows are `None` and never zero, rates
divide by observed duration rather than nominal width, partial / gapped /
degraded / warming windows never teach a baseline, and runs from different
endpoints refuse to combine.

## Evidence and interpretation

Every sensor is two functions with deliberately different arguments:

```python
select(source, scope)       -> EvidenceSet    # no config parameter
interpret(evidence, config) -> list[Finding]  # no source parameter
```

Enforced by signature inspection, not convention
(`test_selection_cannot_see_a_config_and_interpretation_cannot_see_a_source`).
Consequences:

* `evidence_id` is invariant under any analysis config. Re-run with different
  thresholds and the evidence commitment is byte-identical.
* `select()` returns the **whole scope**, including the windows that supported
  no finding — which is what makes cherry-picking detectable on replay.
* An episode's identity tracks its evidence segment, not its thresholds. Two
  configs selecting the same segment produce the same episode with a different
  `config_hash`.

`DetectionEnvelope` is vendored from `driftwatch/src/labeler/detection.py` with
exactly one additive delta (`VALID_SUBJECT_TYPES` gains `"episode"`).
`test_envelope_parity.py` imports driftwatch's module directly and fails if the
two drift apart in fields, constants, public API or hash output.

## What this is not

No account scores, no behavioural forecasts, no arbitrary-handle lookup, no
leaderboards, no public pages, no "who is bad" view, and no type string naming
a mechanism. See [`BOUNDARIES.md`](BOUNDARIES.md) — those are enforced by test,
and the list of things that would need re-justification is in there too.

## Tests

```bash
pytest tests/social -q     # 100 tests
pytest -q                  # 312, whole instrument
```
