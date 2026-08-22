# Candidate work — filed, not authorized

Handles for review. Filing is not permission to build; each needs a forcing
case and acceptance criteria before anything is implemented.

---

## C1 — Split the `unclassified` taxonomy

**Status:** candidate. The misleading *presentation* was fixed on 2026-08-09;
the underlying key split was not.

### What `unclassified` actually is

Measured on the live database, 9,738,714 observed events:

| key | recorded | source |
|---|---|---|
| `unclassified.collection` | **121,312** | two distinct causes, merged |
| `unclassified.operation` | 0 | never fired |
| `unclassified.kind` | 0 | never fired |
| `malformed.commit` | 0 | never fired |

`parse_errors`, `rejected_no_time_us` and `late_events` were **all zero** over
the same 9.74 M events. So `window_health.unclassified` is, in practice,
exactly `unclassified.collection`.

**Update 2026-08-09:** four of the five taxonomy categories turned out to be
*already* separately persisted — `parse_errors`, `rejected_no_time_us` and
`late_events` are their own `window_health` columns, and "unknown schema" is
the sum of three separately-keyed metrics (`unclassified.operation`,
`unclassified.kind`, `malformed.commit`). The dashboard now presents the full
five-way split with no collector or schema change. Only the last category
remains imprecise, which is what the key split below would fix.

`classify._classify_commit` returns that one key from two different places:

* a commit whose `collection` is missing or empty — a genuine
  observer/schema failure;
* a commit from a valid collection outside `COLLECTION_ALIASES` — a **product
  scope** decision, not a failure.

M0 measured `collection` present on 197,926 of 197,926 commits, so the first
cause is empirically zero and the 121,312 are the second. That made the
dashboard's old "Loss buckets: … unclassified 121,312" line materially
misleading: it read as 121k observer failures when the observer had failed
zero times. **Fixed in presentation** — untracked vocabulary is now its own
line, stated as product scope.

### The remaining change (not made)

Split the key so the two causes are separable *going forward*:

```python
if not isinstance(collection, str) or not collection:
    return ["unclassified.collection"]     # observer/schema failure
alias = COLLECTION_ALIASES.get(collection)
if alias is None:
    return ["untracked.collection"]        # product scope
```

Three lines, no schema migration (`metric` is a free-text dimension). It was
not done during the continuous-deployment campaign because it changes
collector output mid-flight, and the honesty problem it addresses was already
solved at the presentation layer.

### Historical rows cannot be retro-split

Both causes share one key, so past rows cannot be separated after the fact.
They should be preserved as legacy `unclassified.collection` and read as
"overwhelmingly untracked vocabulary, with a theoretically possible but never
observed admixture of malformed commits." Do not fabricate a backfill.

---

## C2 — Anonymous ATProto vocabulary weather

**Status:** candidate. **Not feasible from current aggregates** — would need
new persisted data.

Untracked events are ~1.3% of the stream and rising or falling with three
different phenomena that raw volume conflates:

* overall Jetstream activity growth,
* popularity change in existing untracked collections,
* genuinely new collections appearing.

Calling a rising untracked count "vocabulary drift" without separating those
would be wrong. Candidate primitives that would separate them:

* untracked events / all observed events
* distinct collection NSIDs observed per interval
* first-seen NSIDs per interval
* tracked-vs-untracked share
* concentration of volume across NSIDs

### Why it is not free today

Only a single scalar total is persisted; per-NSID counts are not. Getting
these requires persisting collection NSIDs, which is precisely the
unbounded-cardinality dimension deliberately kept out of `metric` in M2. A
bounded design would be needed — a separate small table keyed by NSID, or a
top-N-plus-remainder scheme — before any of this is measurable.

Collection NSIDs are **protocol vocabulary, not user identity**, so this does
not breach the privacy boundary: no DIDs, no handles, no record keys, no
content, no actor graph. It must not become a "who uses this Lexicon" surface;
it is semantic-surface telemetry only.

---

## C3 — Explicit fresh-observation path after cursor-horizon loss

**Status:** candidate. Failure mode identified during continuous deployment.

If a relay accepts the connection but never delivers events for a cursor it no
longer holds, the collector sits connected and silent: nothing is corrupted —
no window opens, no cursor advances, nothing false is recorded — but it does
not self-recover, and `systemctl status` still reads *active*.

There is no CLI flag today to begin a fresh observation without hand-editing
the `meta` cursor row. A narrow `--from-live` / `--new-observation` opt-in that
records the discontinuity as a hard seam would close it. Deliberately not
improvised during deployment work.

Measured 2026-08-09: a **4.41 h** cursor replayed instantly on
jetstream1.us-east, extending M0's verified ≥1 h horizon. The point at which
the horizon actually ends is still unknown.

---

## C4 — The report has no window, and it just hit its first ceiling

**Status:** candidate. A **stopgap is already deployed**; the actual decision
is not made. Filed 2026-08-22 during social-lane activation.

### What happened

Continuous 60 s collection crossed **20,000 span windows** (observed plus gap)
and `weatherwatch-publish` began failing:

```
QueryTooLarge: 20006 windows requested (max 20000). Narrow the range:
silently truncating would turn an incomplete series into one that looks complete.
```

Last successful render: 19,821 windows at 17:34Z. The guard is correct — it
exists so an accidental unbounded query cannot quietly truncate a series into
one that merely *looks* complete — but it was written for accidental callers,
and the report asks for the whole observed interval deliberately and discloses
that interval on the page.

Note the trigger is the **span**, not the row count: gaps count toward it, so
13,730 s of accumulated gap arrived ahead of the observed windows.

### The stopgap that is deployed

`report.REPORT_MAX_WINDOWS = 200_000`, passed explicitly to
`query.total_events_series` and `query.series`. This buys roughly **139 days**
at 60 s. It is a repair to a live outage, not a design, and it is deliberately
a named constant with the reasoning attached rather than a quiet bump to the
library default — the default still protects every other caller.

### The measurement that says a stopgap is not enough

Rendered at ~20,000 windows, on the deployed database:

| | bytes |
|---|---|
| `index.html`, **without** the social section | **10,158,030** |
| `index.html`, with it | 10,397,008 |
| the social section alone | 237,499 (2.3%) |
| `summary.json` | 3,328,854 |

66,792 `<rect>` elements, essentially all of them the observation-health strip
and the sparklines drawing one mark per window. The page grows without bound
in the window count, and the social section is not why. A 10 MB dashboard is a
defect whatever is in it.

### What would actually fix it — none of it chosen

* a **trailing window** for the page (say 7 or 14 days), with the full history
  still reachable in `summary.json`;
* **coarser buckets for the long tail** — minute resolution recently, hourly
  beyond some age — which changes what the sparklines mean and needs saying
  on the page;
* **downsampling the marks** rather than the data: one rect per rendered pixel
  column instead of one per window. Cheapest, changes no stated figure, and
  does not answer the growth question.

Not chosen here because every option changes what the published page *claims*
about its own interval, and that is a product decision rather than a repair.
Whoever takes it should also decide whether `summary.json` follows the page's
window or keeps carrying everything.

**Do not let the 200,000 sit here silently.** It is a ceiling with a date on
it: at 60 s windows it is reached around **2027-01**, and the failure mode is
the publish timer going red again.

---

## C5 — Give weatherwatch its own FQDN

**Status:** candidate, raised 2026-08-22. Not scoped, not scheduled.

Weatherwatch is served as *paths* inside Labelwatch's Caddy site block —
`/weatherwatch` plus the `/beef` alias — rather than from a host of its own.
It has since grown a second published artifact (`social.json`) and a section
that makes its own claims, so borrowing another instrument's origin is
starting to misrepresent what it is.

Target shape: `weatherwatch.neutral.zone`, the way `labelwatch.neutral.zone`
works.

What it touches, none of it decided:

* DNS, plus a certificate — Caddy will want to solve ACME for the new name.
* The Caddyfile: one 86-line file, **seven site blocks, not under version
  control**, host convention is timestamped `Caddyfile.bak` copies. The
  residual risk named in `deploy/README.md` applies unchanged — a malformed
  config takes all seven sites down, so validate before reload.
* `WW_PUBLIC_URL`, and the share card's absolute image URL with it.
* The `X-Robots-Tag: noindex, nofollow, noarchive` posture. A siloed path that
  nothing links to and a bare hostname are different exposure decisions, and
  moving is the moment to make that one deliberately rather than by
  inheritance.
* Whether `/weatherwatch` and `/beef` keep answering — redirect, dual-serve,
  or retire. `/beef` is a published joke alias with its own 301; someone has
  it bookmarked.

Cheap to do, and worth doing before anything else starts linking to the path
form. Not a prerequisite for anything currently shipped.

### In progress, 2026-08-22

**DNS provisioned** by the operator; not yet propagated at the time of
writing (`weatherwatch.neutral.zone` did not resolve from the workstation or
from the serving host).

**Caddy site block added and live.** Appended to
`/home/jbeck/atproto/Caddyfile`, backed up first as
`Caddyfile.bak.20260822-222533` per host convention, `caddy validate` run
*before* the reload because the file carries seven sites and a malformed
config takes all of them down. Post-reload all seven were re-checked and
answer as before. `/beef` is kept as an alias on the new host, redirecting to
`/`.

**Robots posture carried over deliberately, not inherited.** The new host
keeps `X-Robots-Tag: noindex, nofollow, noarchive`. A siloed path nothing
links to and a bare hostname are different exposure decisions; this one is
still open, and carrying the conservative setting forward is not the same as
choosing.

**Redirect staged, not applied.** `/weatherwatch` and `/beef` under
`labelwatch.neutral.zone` still serve. Redirecting them to a hostname that
does not resolve yet would take the published report offline, so the flip
waits until the new host answers 200 on its own certificate. Applying it means
replacing the `@weatherwatch handle` block with a
`redir @weatherwatch https://weatherwatch.neutral.zone{re...} 301` and leaving
`@beef_legacy` pointing at the new host.

Remaining after the flip: decide the robots posture, and decide whether
`summary.json` / `social.json` consumers need the old path kept alive
indefinitely or on a sunset.

---

## C6 — Conversational storm detection, and the map that has no substrate

**Status:** candidate, raised 2026-08-22. Nothing built, nothing authorised.

The idea: extend episode detection from *event-rate* departures to
*conversation* dynamics — reply bursts, quote cascades, participants arriving
and leaving, interaction loops, persistence — and give it a map-like surface.

The framing is right, and it is the reason to take it seriously: **weather does
not accuse.** A storm warning says rotation was observed; it does not say the
cloud meant it. That is the same posture section E already holds, and it is
the posture that keeps this from becoming a witch-finder with a nicer palette.

### What is genuinely close

Reply and quote edges are observable and were verified live (see
`../M0-ADVERSARIAL-DISCOURSE-GRAMMAR.md` §5): `.reply.parent` / `.reply.root`,
and quotes via **two** distinct record paths — `.embed.record.uri` and
`.embed.record.record.uri`, 147 vs 16 on the probe post, so querying one
undercounts. From those, these are episode-shaped and need no new authority
model:

* conversation velocity — replies/min, participants/min
* loopiness — repeat interaction between the same participants, reply depth,
  cycles in the interaction graph
* persistence — does it cool, or keep branching
* participant novelty — share of participants not seen in the prior window.
  Admissible as an **episode-level aggregate**; the same number computed per
  account is a dossier row.

### What is not close, and should not be assumed

**1. There is no geography in ATProto.** A globe has no substrate. Nothing in
the protocol carries location, so a hotspot map is one of: fabricated
coordinates (inventing data, which this estate has never done), or PDS-host IP
geolocation — which is *infrastructure* location, not people, and is a new
identity-adjacent join of exactly the kind `nebgraph/ETHICS.md` is about. A
map is still possible, but it has to be honestly **not Earth**: graph layout,
or an abstract pressure field that never implies a place. Do not ship a globe
that implies people are somewhere.

**2. Topic drift and receipt density need text.** Weatherwatch cannot retain
it: `classify()` discards it structurally, the edge store has no column for
it, and a canary test enforces both. That is the *opposite* retention posture,
and the argument in `M0-ADVERSARIAL-DISCOURSE-GRAMMAR.md` §6 — separate repo,
copy the parsing, import nothing — applies unchanged.

**3. Volume.** Reply/quote edges mean retaining post edges: ~39/s posts and
~17.6/s replies as observed, against the ~5/s the sink retains today. That is
a retention decision before it is a detector decision.

### Naming, decided in advance

Banned from any surface, for the same reason the current type strings avoid
mechanisms: *toxicity map*, *bad actor hotspots*, *harassment clusters*,
*controversy detector*. If this is built, extend
`test_no_type_string_names_a_mechanism` to cover the new vocabulary before
writing the detector, not after.

Admissible: interaction density, conversational turbulence, structure-observed,
unusual graph behaviour. Meteorologist, not judge.

### Gate

Needs a forcing case and acceptance criteria like everything else here. The
honest first increment is not a detector — it is measuring the **base rate**
of reply/quote bursts, so "storm" has a denominator before it has a name.
