# Social Weather — decision record

What this instrument measures, what it deliberately cannot, and why the
absences are design properties rather than a backlog.

Status: current as of 2026-08-22. Supersedes nothing; extends
[`README.md`](README.md) and inherits every boundary in
[`../BOUNDARIES.md`](../BOUNDARIES.md).

---

## 1. What it measures

Nine quantities per window, all derived from weatherwatch's aggregate minute
counters:

| | |
|---|---|
| **velocity** | `emission_velocity`, `interaction_velocity` |
| **pressure** | `interaction_pressure` (reactions per new post) |
| **composition** | `reply_share`, `boundary_share`, `withdrawal_share` |
| **graph activity** | `graph_velocity` |
| **variability** | `turbulence`, `acceleration` |

Plus, derived across windows: **persistence** — how many consecutive recent
windows have held above their hour-typical level.

Each quantity carries its own `non_claim`, and each *shown* measurement is
paired at render time with the inference it does not license. That pairing is
not a disclaimer block: a reader given "interaction activity is 4.6× typical"
supplies "people must be angry" themselves unless the instrument says
otherwise in the same breath.

## 2. What it cannot measure

Four permanent absences, carried on **every** observation record as
`structural_absences` and inside the hash that identifies it — you cannot
strip the statement of limits and keep the identity.

| absence | why it is permanent |
|---|---|
| **participants** | The counters aggregate events, not accounts. Any per-participant quantity, including newcomer ratio, would require retaining actor identity across windows. |
| **location** | ATProto exposes no geography. Inferring it from PDS or IP data would describe infrastructure while implying people. |
| **content** | Record bodies are discarded at the classifier and never stored. |
| **identity** | No actor, target, handle or record key at any point. |

### Participant turnover, specifically

It is the most natural thing to want here — *are these new people?* — and it
is exactly what cannot be answered. It is therefore reported as a blind spot
and **not approximated**. No proxy is offered, because a proxy for an
unmeasurable quantity is a guess wearing a number's clothes.

This was a live decision, not a hypothetical: the brief that prompted the
public layer offered "participant turnover is elevated" as an example reason
to display. It is instead named under *What this instrument cannot see*,
rendered beside the conditions.

## 3. The state model

Eight states, each rendered as **icon + label + one sentence** (never an icon
alone — an emoji by itself is a mood, not a reading), assigned by published
criteria over named inputs, evaluated in order, first match wins:

| | | |
|---|---|---|
| ☀️ | Calm | within the normal range for this time of day |
| ⛅ | Active | above typical, not unusually so |
| 🌬️ | Unsettled | short bursts or shifts, not sustained |
| 🌧️ | Turbulent | elevated activity is persisting |
| ⛈️ | Storm | a significant interaction event is in progress |
| 🌪️ | Severe storm | substantially outside the normal range |
| 🌫️ | Conditions unavailable | cannot support a trustworthy reading |
| 📡 | Station offline | no current measurement at all |

Standard weather grammar, because people already read it: sun = normal,
cloud = unsettled, rain = degraded, thunder = severe, fog = cannot see.
Deliberately coarse — no mechanism-specific icons (no "block tornado", no
"quote storm") while the climatology is still thin. No fire emoji: *everything
is on fire* is funny until the instrument sounds like a meme account.

**Two null states, not one.** `Conditions unavailable` means the instrument
ran and cannot interpret what it got. `Station offline` means it produced no
reading at all, or its newest complete reading is more than 15 windows old.
Collapsing them would hide the difference between a quiet network and a
stopped collector — identical readings, opposite meanings — so staleness is
measured against wall clock and reported as a fact about the instrument.

**A standing refusal rides on every state**, including Calm and both null
states: *not observed — user intent, emotional state, correctness,
coordination, culpability, or geographic origin.* The temptation to over-read
is strongest when the reading is dramatic, but a reader shown `Calm` will just
as happily conclude "so the network is healthy", which is equally unmeasured.

**A state label is a composite, and this estate has refused composite severity
indices.** The published weather page says the Global Beef Index is a joke
name for a composite that does not exist. So the label is built the way a
weather warning is built: a severe thunderstorm warning is not a mood or a
score, it is issued when wind reaches a stated speed, and the criteria are
printed where anyone can check them.

`conditions.CRITERIA` is that table. It is rendered on the public page
verbatim, every state carries the rule that produced it, and a reader who
distrusts the label can reconstruct it. **If the table and the code ever
disagree, the table is the bug.**

Two properties matter more than the thresholds:

- **Persistence separates a gust from a storm.** A single elevated window is
  `Unsettled`; `Turbulent` and above require the reading to hold across
  windows. That distinction is the whole reason `Unsettled` exists as a state
  rather than being folded into `Active`.
- **`Conditions unavailable` never degrades to `Calm`.** "We cannot tell" and
  "nothing is happening" are different facts and only one of them is
  reassuring. Missing data, an unobserved window, or a baseline too thin to
  compare against all produce *unavailable* with the reason attached.

## 4. Climatology before detectors

No detector has been built and none should be until the baseline can carry
one. The order is climatology → observation → candidates → *(not built)*
detectors, because starting at "detect storms" is how "unusual" gets defined
by whoever picked the threshold.

Two sample sizes are reported because the window count is neither:

- **`n_eff` of the deseasonalised residual**, not the raw series. A smooth
  daily cycle alone pushes lag-1 *r* above 0.95 and drives raw `n_eff` into
  single digits; conditioning happens inside an hour cell, so the residual is
  the series whose independence governs. Both columns are shown, and the gap
  between them is the daily cycle, not information.
- **Independent replicates.** For "unusual for this hour of day" they are
  **days**; for hour of *week* they are **weeks**, and a fortnight gives two.
  Hour-of-week is computed, reported, and marked unsupported rather than
  quietly used.

Quantities whose support is `unsupported` are excluded from candidates and
from the public state entirely.

## 5. Station page disposition

**Decision: operator-only for now. Not published.**

The calibration surface (`render_station`) and the public weather page
(`render_public`) are separate artifacts written to separate directories by
separate flags, and the CLI refuses to write the calibration surface into the
public output directory.

Three options were considered:

| option | for | against |
|---|---|---|
| **Public documentation** | An outside expert could check the instrument, which is the trust claim the whole design rests on. | It is ~30× the size of the public page and reads as a dashboard. Publishing it makes raw metrics the product, which objective 3 exists to prevent. |
| **Operator-only** | Keeps the visitor experience clean and the state model the headline. | The "an expert can inspect it" claim becomes unverifiable from outside. |
| **Separate technical page** | Both audiences served, neither surface compromised. | Two published surfaces to keep honest; the technical one will be read as authoritative regardless of its caveats. |

**Chosen: operator-only, with a stated route to the third option.** The
deciding factor is that the instrument is *currently under-calibrated* — most
quantities score `thin` on the live estate. Publishing a calibration surface
whose honest verdict is "not enough history yet" as the instrument's public
face invites exactly the misreading the campaign is trying to avoid, and it
would be read as the product rather than as the paperwork.

**Trigger for revisiting:** when `interaction_velocity` reaches `supported`
(≥20 day replicates), publish the **baseline report**
(`climatology.md`) — not the chart page. The report is the better artifact for
the expert-check role: it is prose, it states its own limits in the same
prominence as its findings, and it cannot be skimmed as a dashboard. The chart
page stays operator-only indefinitely unless a specific need appears for it.

Nothing about this is reversible-by-accident: publishing either surface
requires a deliberate flag and a deliberate path.

## 6. What would need re-justification

1. Approximating any structural absence, including with a proxy or a sketch.
2. Publishing the calibration chart page.
3. A state whose name refers to a person, a group, or a motive; or a
   mechanism-specific icon.
4. Any detector built before its quantity's climatology is `supported`.
5. Any map with a geographic reading, however abstract the projection.
6. Removing the paired *not observed* column from a shown measurement, or
   moving it behind a disclosure element the reader can collapse.
7. Any forecast, projection, or trend arrow. The recent-conditions strip is
   history; a page that predicts weather is making a claim about the future of
   a public field, which is a different instrument entirely.

Update this file before shipping any of them, not after.

## 7. The conditions became the first screen of the canonical page

**Decision: merged into the report, 2026-08-25. One builder, two consumers.**

Until this point the instrument that answers *what is the network doing right
now* existed, was disciplined, and was **not on the published page at all**.
`weatherwatch.neutral.zone` opened with observation status: run history,
ingest accounting, lag EWMA. A visitor met the instrument's paperwork before
its reading, and the reading was on a page nothing published.

The conditions block is now rendered at the top of the report, above a
"receipts" deck holding the six lettered sections that were previously the
whole page. Nothing was deleted; the sections fold.

Three properties make this a presentation change rather than a semantic one:

- **One builder.** `hero.render` is called by the report, by
  `viz.render_public` and by `viz.render_station`. The duplicate renderer that
  would otherwise drift is the same failure `criteria_table()` was introduced
  to stop, one level up.
- **The report reads, it does not compute.** Sealing a fortnight of minute
  windows is seconds of work and the publisher runs every five minutes, so the
  report loads sealed observations from the store. `weatherwatch social field`
  is the writer, on its own timer (`weatherwatch-field.timer`). No new
  retention: the field is derived from counters that already exist.
- **Every failure is `station_offline` with the reason attached.** No store
  configured, no baseline sealed, nothing filed against it, store unreadable —
  each says which. None of them says Calm. This is the same rule as
  *unavailable is not calm*, applied to the transport rather than to the
  climatology.

The two lanes stay separate and are reported separately: the aggregate
ingest lane can be `current` while the field archive is `station_offline`,
and the page says both. Averaging them into one "health" word would hide
exactly the case the second state exists to name.

**What did not change:** no threshold, no criterion, no climatology, no state
vocabulary, no custody, no detector. The state a reader sees is the state
`assess` returned, and the criteria table is still rendered verbatim.

**Also under this decision:** the *recent conditions* strip is history and is
derived by re-running the same `assess` over a prefix of the same
observations. It cannot disagree with the headline about what a state means,
and an interval with no eligible window reports **no observation** rather than
carrying the previous reading forward. There is no forecast, and none should
be added without re-justifying this section.

## 8. Identity, idempotence, and the cadence that makes freshness true

**Decision: identity is the fitted baseline; the extent is evidence.**
Repaired 2026-08-25 after a measured production incident.

### What broke

The sealer re-fits its climatology over a *trailing* range, so `ts_start` and
`ts_end` move on every run by construction. `climatology_id` hashed over them;
`observe()` stamps that id onto every observation; the observation hashed over
that. So one hourly reseal minted 43,201 logically identical observations under
fresh identities — about **175 MB per run**, taking the live store from 474 MB
to 827 MB in two runs, roughly **4 GB a day** of duplicate epistemology.

It was fixed at the identity layer and **not** with retention. A producer that
manufactures logical duplicates is broken whether or not something downstream
deletes them efficiently; retention is an orthogonal policy and is owned
elsewhere.

### What constitutes identity now

**A climatology is its fitted baseline.** In the hash: schema version, window
width, the distributions themselves (percentiles, spread, support), and the
observer they came from. Two runs that fit the same numbers are the same
baseline whatever slice of wall clock produced them; a run whose percentiles
genuinely moved is a different baseline and gets a different id.

Excluded — `climatology.EXTENT_FIELDS` — and still present in the document:
`ts_start`, `ts_end`, `n_windows`, `n_observed`, `n_eligible`, `n_days`,
`n_weeks`, `hour_of_week_supported`, `hour_of_week_note`, and
`provenance.run_ids`. A reader still learns exactly which interval was fitted
and over how many day replicates. It simply does not fork the address.

**An observation is what was observed.** In the hash: the schema, the window
and its bounds, the metrics, what could not be measured and why, the window's
*own* observation quality (`coverage`, `eligible`, `quality` — a window
re-observed differently is a different observation), the observer, the
non-claims, and `structural_absences`. That last remains load-bearing: you
still cannot strip the statement of what this instrument cannot see and keep
the same identity.

Excluded — `observation.CONFIDENCE_EVIDENCE_KEYS` and
`PROVENANCE_EVIDENCE_KEYS` — and still present in the document:
`baseline_days`, `baseline_n_eff`, `support`, `note`, `run_ids`,
`climatology_id`, `climatology_days`, `hour_of_week_supported`. Re-scoring an
unchanged window against a fresher baseline does not make it a different
observation of the network; it makes it the same observation carrying newer
evidence.

`identity_document()` is a single builder used both to mint an id and to replay
one. Two ways to compute one identity is two identities.

### Idempotence, measured

On 30 days of minute windows (43,200 observations):

| | rows added | bytes added |
|---|---|---|
| first seal | 43,200 | 181 MB |
| reseal, no new data | **0** | **0** |
| advance one window | **1** | one row |

`save_observations` additionally writes only rows whose document actually
changed, and returns `{considered, written, unchanged}`. On a healthy reseal
`written` is the number of windows that arrived. **If `written` ever equals
`considered` over an unchanged range, the archive is forking again.**

`sealed_at` is deliberately not refreshed on an unchanged row: it records when
an observation was sealed, not when it was last re-confirmed.

### Freshness and producer cadence

`STALE_AFTER_WINDOWS` is 15 and the deployed window is 60 s, so a reading older
than 15 minutes is reported as `station_offline` — correctly, because that is
what the published criteria table says.

The sealer ran **hourly**, so the public page truthfully reported the station
offline for roughly 45 minutes in every 60. The rule was right and the cadence
was wrong. **The producer is aligned to the declared semantics rather than
freshness being redefined around a cron entry**: the timer now runs every 5
minutes, giving a worst-case reading age of about 5.5 minutes against a
15-minute budget, so two consecutive runs can fail before a reader is told the
station is offline. `tests/test_report.py` pins that relationship so neither
side can drift away from the other.

This is affordable only because sealing became idempotent: a steady-state run
adds no rows and no bytes, and costs about 21 s, almost all of it re-fitting
the climatology and reading existing documents back to compare.

### Legacy rows

Observations sealed under the old identity semantics remain in the store and
are superseded rather than migrated. Cleaning them up is a retention decision
and belongs to whoever owns storage policy; nothing here deletes, prunes or
vacuums anything.
