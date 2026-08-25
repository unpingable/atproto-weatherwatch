# weatherwatch

[![CI](https://github.com/unpingable/atproto-weatherwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/unpingable/atproto-weatherwatch/actions/workflows/ci.yml)

An aggregate ATProto/Bluesky event weather instrument: it counts network-level
activity rates from a Jetstream source and keeps no people. (The name began as
a disposable handle; it is now load-bearing — deployed path, published URL,
`/beef` redirect.)

Measured protocol behaviour: [`M0-VERIFICATION-RESULTS.md`](M0-VERIFICATION-RESULTS.md).
The paired-observer result also has a concise standalone note:
[`Public Jetstream observers were not interchangeable in one paired probe`](docs/JETSTREAM-OBSERVER-DIVERGENCE.md).

**Published at <https://weatherwatch.neutral.zone/>** since 2026-08-22; the
former `labelwatch.neutral.zone/weatherwatch` path and the `/beef` alias both
301 there, so existing references keep working.

**Status: M0–M7 implemented; collector supervised under systemd; static report
published at <https://weatherwatch.neutral.zone/> (`/beef` 301s to
it).** There is still no HTTP server, no API, and no read endpoint in this
codebase — the collector writes a local SQLite file and the report is static
HTML generated into a directory, which is then published. Run it from a
terminal.

**What the published numbers are entitled to claim.** Rates are *as observed at
one named Jetstream instance*, not network truth. M0 falsified relay
interchangeability (§Item 11: two same-region endpoints differing **1.61×** in
post volume, same-endpoint self-control 1.000). The deployed collector runs on
`jetstream1.us-east` — the higher-observing side, which makes the numbers larger
without making them global. Local z-scores stay self-consistent against a single
observer; **absolute rates do not**, in either direction. Observed-volume ratios
are inter-observer comparisons, never coverage figures: there is no canonical
denominator. A second collector and any multi-observer reconciliation remain
out of scope.

## Social episode sensors (`weatherwatch social`)

An analysis layer over this same observation lane: it segments the buckets
already persisted here into **episodes** — block bursts, like/repost storms,
lulls — and, behind an opt-in edge sink, adds concentration, overlap,
synchronisation and account-lifecycle co-occurrence. The subject of every
detection is the episode, never an account.

The weather lane is unchanged and still keeps no people. Edge custody is off
by default and writes a separate database. The current deployment explicitly
enables a narrow `block,listitem` edge sink with 24-hour retention; those
identity-bearing rows and local findings are never published. Public aggregate
episodes additionally fail closed unless the local store witnesses a
provisional 10-actor support floor **and** — for a rate excess — no single
actor could have produced the whole departure, then expose only
hour-coarsened, reduced fields. This is disclosure resistance, not anonymity;
what each gate does and does not establish is set out, with the adversarial
case that forced the second one, in
[`BOUNDARIES.md`](src/weatherwatch/social/BOUNDARIES.md). Overview:
[`src/weatherwatch/social/README.md`](src/weatherwatch/social/README.md).

## What it does

```
Jetstream (one named endpoint)
  -> classify transiently          identity read, never returned
  -> increment in-memory counters  63 possible keys, no free text
  -> close 60s windows by stream clock
  -> ONE transaction: buckets + window health + resume cursor
```

What the weather database persists: integer counts per (run, window, metric),
an observation health row per window, and a resume cursor. That is all.

What the weather database and public artifacts never contain: raw events,
DIDs, handles, rkeys, CIDs, event-supplied AT URIs, post text, display names,
descriptions, alt text, or event-supplied URLs. The separately enabled,
bounded social edge sink deliberately
retains the minimal identity-bearing edge fields documented in
[`BOUNDARIES.md`](src/weatherwatch/social/BOUNDARIES.md); it is local-only.

## Privacy model

The classifier is the identity boundary, and the guarantee is structural
rather than filter-based: its output alphabet is a **finite frozenset**
(`classify.ALLOWED_METRICS`, 63 entries). A DID cannot appear in the output
because no DID is a member of that set. Tests assert the containment against
the whole fixture corpus, and further tests assert that nothing identity-shaped
reaches the weather database or any public artifact.

Test fixtures are scrubbed structure captured during M0 — synthetic DIDs use
the reserved `did:example:` method and synthetic hosts use RFC 2606
`.invalid`. `spike/check_fixture_privacy.py` is the tripwire.

## Observation runs

This is an instrument that *may* run continuously, not a daemon that must.

| | |
|---|---|
| outside a run | NOT OBSERVED — no row exists |
| inside a run, quiet | OBSERVED, EMPTY — row exists with `events_seen = 0` |
| inside a run, degraded | OBSERVED, CONDITIONED — `coverage_state = degraded` |
| missing interval in a run | GAP — `gap_us`, `resume_seam` |

A machine being off between runs is not a failure and produces no fake missing
windows. Partial first/last windows record their real observed duration and
are flagged `partial`; they never masquerade as full ones.

## Coverage claim

    Aggregate activity observed from this Jetstream source during this
    observation interval.

M0 falsified the assumption that public Jetstream instances are
interchangeable complete views: `jetstream1.*` delivered ~1.6x the post volume
of `jetstream2.us-east` in a concurrent window, while two sockets to the same
instance agreed exactly. So:

* every run binds to one exact endpoint;
* a cursor from one endpoint is never used against another;
* changing endpoint starts a new run — a hard observation seam;
* runs from different endpoints, or overlapping in time, refuse to be summed;
* no relay is described as complete or as ground truth. Higher volume is not
  evidence of greater completeness.

## Usage

```bash
pip install -e .

weatherwatch collect                       # unbounded, Ctrl-C to stop
weatherwatch collect --duration 30m
weatherwatch collect --endpoint jetstream2.us-east --duration 1h

weatherwatch runs                          # observation runs and coverage
weatherwatch stats                         # metric totals and rates
weatherwatch series post.create            # per-window, with conditioning
weatherwatch ratios                        # reply/post, block/follow, ...
weatherwatch correlate post.create.quote block.create
weatherwatch report --output ./beef        # static dashboard
```

Read commands default to the most recent *compatible* sequence of runs on one
endpoint. Asking for an incompatible combination fails loudly rather than
producing a number that describes no actual observation.

Default endpoint is `jetstream1.us-east` — the higher-volume endpoint M0
measured. Configurable, and the choice is recorded on every run.

Keep the SQLite file on local disk. Never NFS/SMB/NAS.

## Layout

```
src/weatherwatch/
  classify.py     the identity boundary; pure, finite output alphabet
  accumulator.py  window assignment, coverage accounting, commit invariant
  db.py           four tables and the atomic flush
  health.py       coverage state machine (adapted from driftwatch)
  collector.py    the asyncio loop, reconnect/replay discipline
  read.py         the "not summable" guard
  query.py        read side: series, run summaries, conditioning flags
  derive.py       read-time ratios, rolling baselines, z-scores, correlation
  report.py       static dashboard generation (atomic directory swap)
  cli.py
spike/            M0 throwaway probes — do not build on these
fixtures/         scrubbed structural fixtures + synthetic hostile cases
measurements/     M0 aggregate measurements
```

## Tests

```bash
python -m pip install -e ".[dev]"
./scripts/qualify.sh
```

This is the authoritative local and CI qualification: it compile-checks the
source and tests, runs the fixture privacy tripwire, then runs the complete
test suite. It includes end-to-end collector tests against an in-process fake Jetstream that
reproduces the inclusive-cursor semantics M0 measured, so `cursor + 1` is
exercised rather than assumed.

## License

Licensed under either Apache-2.0 or MIT, at your option. See
[`LICENSE-APACHE`](LICENSE-APACHE) and [`LICENSE-MIT`](LICENSE-MIT).

## Conditioning

Every rate divides by *observed* duration, never nominal window width. Every
series distinguishes three states that are easy to confuse and expensive to
confuse:

| | |
|---|---|
| `count = 0` | observed, genuinely no activity |
| `count = None` | **unobserved** — nobody was watching |
| `quality != clean` | observed but conditioned: partial, gap, loss, degraded |

Nothing is interpolated across unobserved time, and no baseline learns from a
window with a measured completeness defect. Latency is tracked separately from
coverage: a collector replaying backlog is far behind real time while missing
nothing, and its counts stay usable.

Derived conditions (`quiet` / `normal` / `elevated` / `surging` / `degrading`)
are threshold cuts on a z-score against a short trailing baseline of the same
stream, with an effect-size floor in front of them: a change smaller than
`derive.MIN_LABEL_EFFECT` is reported as `normal` however significant it is,
because a near-flat baseline makes a 3% move enormously significant and a
reader cannot un-see a red word. Both gates are uncalibrated and carry no
statistical warrant; the z, baseline and percent change are all still printed
unmodified beside the label. There is no Global Beef Index; the dashboard
shows a placeholder marked *calibration not assumed*.

## Product doctrine

    The joke is the disclaimer.
    The composite is descriptive.
    The primitives are authoritative.
    The denominator gets a lawyer.

**The joke is the disclaimer.** If a composite is ever defined it stays named
*Global Beef Index*. The unserious name is deliberate epistemic signalling: a
solemn construct name — Behavioral Turbulence Index, Social Stress Index —
would imply a validated latent variable this system has not earned and cannot
currently earn. Do not professionalise the name.

**The composite is descriptive.** Any future index would be a transparent
composite over aggregate observable behaviour, internally normalised against
its own history. Not externally validated, not causal, not sentiment, not
about individuals, not ground-truth conflict detection.

**Two kinds of calibration, only one of which is available.** *External*
calibration ("this number corresponds to actual beef") has no defensible
ground truth: any corpus of remembered beef is selection-biased toward
spectacle — quote-post pile-ons, famous arguments, public meltdowns — while
block storms, correlated unfollows and deletion waves may carry no historical
label at all. Supervised fitting against remembered events would build a
*spectacle* detector and call it a conflict detector. *Internal* normalisation
("this combination of primitives is unusual against its own regime") is
legitimate, and is the only kind on the table.

The placeholder therefore reads *calibration not assumed*. It does not imply
that validated beef ground truth is merely waiting to be collected, and it
does not claim an uncalibrated formula already exists.

**The primitives are authoritative.** A composite is a hint layer. Nobody
should have to trust `BEEF = 73` without inspecting the aggregate series that
produced it; the 16 primitive cards remain the receipts.

**The denominator gets a lawyer.** Every ratio is a two-body system. The public
table renders each value in one non-wrapping expression with its numerator and
denominator counts, so the support cannot fall away from the ratio visually,
and window extremes are drawn only from windows whose denominator reached
`report.MIN_RATIO_DENOMINATOR` — a legibility floor, stated as such on the
page, not a statistical one. Excluded windows are counted in their own column
rather than dropped. The guard is structural because a prose caveat does not
travel with a screenshot: `block/follow = 22.75` off a four-event denominator
crops into a social index.
`block/follow` can move because blocks rose, because follows fell, because
both moved, or because the denominator got too small to mean anything. Never
narrate a ratio as if only the numerator changed, keep both components
inspectable, and do not treat a metric as a stable baseline merely because it
sits in a denominator. The ratio is the hint; the primitives are the receipts.

**State the negative scope first.** A cold reader — human or model —
arriving at what was then `/beef` read "beef" plus Bluesky plus telemetry and
concluded conflict monitoring. This was observed: an LLM given the page
confidently classified it as a social-drama detector and had to reason its way
back out. Every disclaimer on the page was about *coverage* (which relay, how
complete); none said what is not measured, so there was nothing to correct the
misread with.

Two fixes followed. The page now leads with the denial. And the canonical path
became `/weatherwatch`, because **a joke needs its disclaimer adjacent and a
URL travels alone** — `/beef` still 301s so nothing breaks, but a redirect
renders no text and so primes nothing.

Note which phrase actually caused it. "Global Beef Index" is *obviously*
unserious — that is the joke doing its job. "Cortisol accounting" was the
dangerous one: solemn enough to parse as a real biomarker construct. The
correction therefore leads the page, before either phrase, and `summary.json`
carries `measures` / `does_not_measure` so a script never has to read prose.

**Narrative restraint.** The instrument reports observable aggregate behaviour
and bounded derived conditions. "Post deletes elevated, blocks down" must not
become "morning-after beef" or "users are deleting evidence". Those readings
may be funny and even right, but the telemetry does not entail them. Humans
may make the joke; the instrument must not certify it.

## Deployment

The public site serves static output from a separate directory, with `noindex`
at both the meta tag and response header. Public-safe deployment architecture,
validation, rollback shape, and publication invariants live in
[`deploy/README.md`](deploy/README.md); credentials, private host paths, and
deployment authority do not. Publish with `./deploy/publish.sh`.
