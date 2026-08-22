# Social Weather — an instrument for the conditions of a public field

A weather service reports atmospheric conditions. It does not decide who is
morally responsible for the storm.

This package answers **what changed in the interaction field**. It is built so
that it cannot answer **who caused it** — not as a policy layered on top, but
because the data it reads never contained a person.

```
Jetstream → collector → classify() → minute counters → field vector
                          │                              │
                   identity boundary              climatology
                   (finite alphabet,                     │
                    no DID can appear)            observations
                                                         │
                                                    station page
```

## What it measures

Nine quantities per window, all derived from aggregate counters:

| quantity | unit | what it is |
|---|---|---|
| `emission_velocity` | events/s | rate of new top-level content records |
| `interaction_velocity` | events/s | rate of reaction records — replies, quotes, reposts, likes |
| `interaction_pressure` | reactions/post | reaction records per unit of emitted content |
| `reply_share` | ratio | share of reactions that were replies rather than likes/reposts/quotes |
| `graph_velocity` | events/s | rate of follow / block / list-membership records, created or deleted |
| `boundary_share` | ratio | share of new follow-or-block records that were blocks |
| `withdrawal_share` | ratio | share of create-or-delete records that were deletions |
| `turbulence` | coeff. of variation | variability of interaction velocity across trailing context |
| `acceleration` | events/s per window | change in interaction velocity from the previous eligible window |

Every quantity carries its own `non_claim`, and an observation's non-claims are
**assembled from the quantities it actually contains** rather than being a
fixed paragraph. `boundary_share` rising means more block records relative to
follow records were observed. It does not mean conflict, hostility, or a
worsening climate, and the name was chosen so it cannot be misread as one.

## What it deliberately cannot measure

These appear in `STRUCTURAL_ABSENCES` on **every** observation:

- **participants** — no count of distinct people. The counters aggregate
  events, not actors, so any per-participant quantity (including the newcomer
  ratio) is unavailable *by construction* rather than unimplemented.
- **location** — no geography. ATProto exposes none, and inferring it from PDS
  or IP data would describe infrastructure while implying people.
- **content** — no text, topic or sentiment. Record bodies are discarded at
  the classifier.
- **identity** — no actor, target, handle or record key.

### Why the absences are a design property

An absent field usually means "not built yet". Here it means the opposite: the
field is absent because measuring it would require retention the instrument
refuses, and adding it later would not be a feature but a change of posture.

Stating that in the data rather than only in prose matters, because a
downstream reader sees records, not READMEs. So:

- `unavailable` is first-class. A quantity that could not be measured in a
  window appears there **with its reason** — unobserved window, zero
  denominator, insufficient trailing context — rather than being silently
  absent or nulled.
- `STRUCTURAL_ABSENCES` rides on every observation and is part of what the
  `observation_id` hash commits to. You cannot strip the statement of limits
  and keep the identity.
- The tests assert the absences rather than the presences:
  `test_no_per_actor_quantity_exists`, `test_no_module_reaches_for_geography`,
  `test_no_quantity_name_or_summary_makes_a_causal_claim`.

## Climatology before detection

The order is deliberate and the package will not skip it:

1. **climatology** — what is normal?
2. **observation** — what were conditions in this window?
3. **candidates** — which windows sit outside their own climatology?
4. *(not built)* detectors

Starting at 3 is how "unusual" ends up defined by whoever picked the
threshold.

### Two kinds of sample size, both reported

A fortnight of minute windows looks like 20,000 samples. It is not.

- **Effective N.** Adjacent minutes are strongly autocorrelated, so every
  distribution reports `n_eff = n(1−r)/(1+r)` alongside `n`.
- **The residual matters, not the raw series.** A smooth daily cycle alone
  pushes lag-1 *r* above 0.95 and drives `n_eff` to single digits — measured
  on a synthetic fixture: raw *r* 0.954 → `n_eff` 4.5, while the
  deseasonalised residual gave *r* −0.13 → `n_eff` 249. Conditioning happens
  inside an hour cell, so support keys on the residual. Both are reported.
- **Replicates, not windows.** To ask "is this unusual *for this hour of
  day*", the independent replicates are **days**. To ask it for hour of
  *week*, they are **weeks** — and a fortnight gives two. `hour_of_week` is
  therefore computed, reported, and marked unsupported rather than quietly
  used.

Support levels: `supported` needs ≥20 day replicates and residual `n_eff` ≥100;
`thin` needs ≥7 and ≥30; otherwise `unsupported`, and unsupported quantities
produce **no candidates at all**.

### Candidates are not detections

A candidate is a window outside the 5th–95th percentile of its own
hour-of-day cell. Two guards keep that from becoming a finding:

- **The nominal rate is published next to the count.** A 5th–95th rule marks
  **10% of windows by construction**. "856 candidates" is mostly the
  definition; the interpretable figure is the excess over 10%, and even that
  is noisy when each cell is estimated from a handful of days.
- **Degenerate cells are skipped.** A near-constant quantity has p05 == p95,
  and without a spread guard every window with float jitter reads as unusual —
  observed on a fixture as 192 of 192 windows flagged.

## Two surfaces, never merged

| | public | calibration |
|---|---|---|
| function | `viz.render_public` | `viz.render_station` |
| flag | `--output` | `--station-output` |
| size | ~11 KB | ~320 KB |
| carries | state, why, limits, radar | meteogram, intensity map, field portrait, climatology, provenance |
| published | yes (when deployed) | **no** — operator only |

The CLI refuses to write the calibration surface into the public directory.
The disposition and the trigger for revisiting it are recorded in
[`DECISIONS.md`](DECISIONS.md) §5.

`--station-output` also writes **`climatology.md`** — the baseline report,
generated from the climatology rather than composed by hand, answering *what
does normal look like?* with the observation window, both sample-size
estimates, seasonality handling, the conclusions the baseline cannot support,
and the permanent blind spots.

## The public layer

A visitor should be able to answer *is this environment calm, active,
turbulent, or under unusual stress right now?* without meeting a z-score.
Three tiers, progressive disclosure, no JavaScript:

1. **The state.** A headline, one plain sentence, and a radar. *"Severe
   interaction storm — interaction activity is running about 4.6× its usual
   level for this hour and has stayed there for 5 windows."*
2. **Why**, as two columns: every measurement shown beside the inference it
   does **not** license.

   | Observed | Not observed |
   |---|---|
   | Interaction activity is 4.6× its typical level for this hour. | That anyone is upset, or that the activity is hostile. |
   | Conditions have stayed elevated for 5 consecutive windows. | That this was coordinated, planned or organised. |

   The pairing is driven off the reasons actually rendered, so the columns
   always have the same number of rows and no measurement can reach a reader
   without its limit. An unmapped quantity gets a generic refusal rather than
   being dropped.
3. **The rule that was applied**, and what the instrument cannot see at all.

States are `Calm`, `Active`, `Turbulent`, `Storm`, `Severe storm`, and
`Conditions unavailable`.

### A state label is a composite — so it is built like a weather warning

This estate has explicitly refused composite severity indices; the published
weather page says the Global Beef Index is a joke name for a composite that
does not exist. A single state label is exactly such a composite, so it is
constructed the way a weather service constructs one: **published criteria
over named, visible inputs.**

A severe thunderstorm warning is not a mood or a score. It is issued when wind
reaches a stated speed or hail a stated size, and the criteria are printed
where anyone can check them. `conditions.CRITERIA` is that table, it is
rendered on the page verbatim, and every state carries the rule that produced
it together with the numbers that satisfied it. A reader who distrusts the
label can reconstruct it. If the table and the code ever disagree, the table
is the bug.

Persistence is what separates a gust from a storm: `Storm` requires the
elevated reading to hold across consecutive windows, so a single spike window
is `Active`, not a storm.

### Unavailable is not calm

When the climatology cannot support the comparison — too few day replicates,
or the window was not observed — the state is `Conditions unavailable` and
says why. It never falls back to `Calm`, because *"we cannot tell"* and
*"nothing is happening"* are different facts and only one of them is
reassuring. On a three-day history every quantity reports `unsupported` and
the page says so rather than reporting a calm field.

### What it refuses to approximate

The brief that prompted this layer offered *"participant turnover is
elevated"* as an example reason. It cannot be measured — the counters
aggregate events, not accounts — so instead of approximating it, participant
turnover is named in **What this instrument cannot see**, rendered next to the
conditions. The absence is part of the reading, not a footnote to it.

## The visualization

Four panels, in abstract space. **No globe.** A globe was considered and
rejected: ATProto exposes no geography, so putting these quantities on a
picture of Earth would invent the one dimension the instrument does not have,
and readers trust maps.

- **Hero radar** — angle is hour of day, distance from centre is activity
  against what that hour usually looks like, on a **log** radius with
  "typical" at mid-radius. Linear spacing squashed the entire interesting
  region (roughly 0.8×–1.3×) into a few pixels and made the usual-range band
  invisible; log spacing also gives quiet conditions somewhere to go, which
  matters for an instrument that reports lulls as readily as storms. Readings
  past the outer ring are drawn on it *and marked*, because a clipped storm
  that looks like a smaller one is a lie of omission.
- **A · Conditions** — current values against their own hour-of-day cell
- **B · Meteogram** — quantities over time, min–max banded per rendered column
- **C · Diurnal intensity** — hour-of-day × date; unobserved cells flat grey,
  never zero
- **D · Field portrait** — abstract (velocity, turbulence) state space,
  density-binned, with windows outside their hour cell ringed

Panel D carries the "something unusual is happening here" reading: ordinary
conditions pile into a dense cloud and an excursion is visibly outside it.
There is no one in that picture, because there is no one in the data.

Every panel is bounded before it is drawn — the meteogram collapses to one
column per pixel, the portrait bins to a fixed grid — so page size is a
function of the chart, not of how long the instrument has run, and both
disclose what they collapsed. See `docs/CANDIDATES.md` C4 for what happens
when a chart draws one mark per window and nobody bounds it.

## Replay

Observations are content-addressed: `observation_id` is the hash of the
canonical document, and the stored document *is* that canonical form, so
re-deriving the id from storage must reproduce it exactly. The station page
renders from stored observations rather than from memory, and rendering the
same store twice is byte-identical.

## Usage

```bash
weatherwatch social field --last 7d --social-db data/social.sqlite
weatherwatch social field --last 7d --social-db data/social.sqlite --output out/station
```

Adds no retention: the field vector reads counters that already exist. It runs
identically whether or not edge custody is enabled.

## What this is not

Not a moderation system, not a reputation system, not a detector, not an alarm.
It reports conditions and how much confidence they can carry — including, and
especially, when the answer is "not much".
