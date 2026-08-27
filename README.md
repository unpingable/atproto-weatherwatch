# Weatherwatch

[![CI](https://github.com/unpingable/atproto-weatherwatch/actions/workflows/ci.yml/badge.svg)](https://github.com/unpingable/atproto-weatherwatch/actions/workflows/ci.yml)

Weatherwatch is an observability instrument for aggregate activity on the AT
Protocol network (the protocol Bluesky runs on). It watches one Jetstream
event stream, counts categories of protocol activity over time, records how
good its own observation was, and renders a static dashboard showing how
those rates change.

It measures the network's observable weather — how much of which kind of
activity is flowing — not individual people, posts, sentiment, arguments, or
social drama.

**Public dashboard: <https://weatherwatch.neutral.zone/>** — static files,
no account needed, no JavaScript required, nothing about a visitor observable
beyond ordinary web-server logs. Published since 2026-08-22.

## What it is

ATProto is a public protocol: when someone posts, likes, follows, blocks, or
deletes, that action is a record written to a public relay network.
*Jetstream* is a public WebSocket service that streams those events, as JSON,
as they flow through a relay.

Weatherwatch connects to one Jetstream endpoint and counts what goes by:

- posts created, and of those, replies and quotes;
- likes, reposts, follows, blocks, unblocks;
- deletes of each of those;
- list-membership changes, profile updates, account and identity events;
- each of the above as a rate per minute, over time;
- and, just as deliberately, the health of the observation itself — gaps,
  degraded windows, lag — so a number never travels without its coverage.

This is closer to network telemetry than to social analytics. The instrument
produces counters and coverage statements; it never reads content for meaning
and it never follows a person.

## What it is not

- **Not sentiment analysis or conflict detection.** A count of block events
  is a count of block events. The instrument has no access to what anyone
  meant, felt, or intended, and it does not infer those things from rates.
- **Not content analysis.** Post text, handles, profiles, and media are
  discarded at the classifier; they are never stored.
- **Not user profiling.** No leaderboards, no account pages, no per-person
  anything. The weather database structurally cannot hold a person (see
  *Privacy and identity*).
- **Not a popularity ranking or a "drama meter."** Nothing here ranks posts,
  accounts, topics, or communities.
- **Not a census of ATProto, and not ground truth.** Every number is what
  one named observer saw during one observation interval. See
  *Observation limits*.
- **Not a moderation, reputation, or alerting system.**

## What you can see

The dashboard at <https://weatherwatch.neutral.zone/> is a static page,
regenerated on a five-minute cadence from the local database:

- **Current conditions** — a headline state (`Calm`, `Active`, `Turbulent`,
  `Storm`, …) built the way a weather warning is built: published criteria
  over named, visible inputs, so a reader who distrusts the label can
  reconstruct it. When the baseline cannot support a comparison the state is
  `Conditions unavailable`, because "we cannot tell" and "nothing is
  happening" are different facts.
- **Sixteen primitive cards** — the observed rates (posts, replies, quotes,
  reposts, likes, follows, blocks, deletes, …) with per-minute sparklines.
  These are the receipts; everything else is derived from them.
- **Condition labels** — `quiet` / `normal` / `elevated` / `surging` /
  `degrading`, which are threshold cuts on a z-score against a short trailing
  baseline *of the same stream*, with an effect-size floor in front of them.
  They are descriptive local-baseline labels, not validated social states and
  not statistically calibrated; the z-score, baseline, and percent change are
  printed unmodified beside every label.
- **Ratios with their support** — e.g. replies per post, blocks per follow —
  always rendered with the numerator and denominator counts that produced
  them, so the support cannot fall away from the number.
- **Observation health** — per-window quality: clean, partial, gap, degraded,
  unobserved.
- **Aggregate social episodes** — a bounded, disclosure-gated list of
  episodes (a block burst, a like storm, a lull) detected over the aggregate
  counters. The subject of every episode is the episode; no account appears.
  See *Privacy and identity*.
- **Findings** — permanent pages for published measurement results, with
  machine-readable aggregate receipts. A finding is a historical publication,
  not current observation state.

Everything is also published as static JSON (`summary.json`, per-day history,
`social.json`, finding receipts) with versioned schemas. The full artifact
contract is in [`docs/PUBLIC-ARTIFACTS.md`](docs/PUBLIC-ARTIFACTS.md).

## Privacy and identity

The short version: the weather database and everything published contain
counts and coverage metadata — no accounts, no posts, no text, no identity of
any kind. The longer version has three lanes, and they are deliberately
different.

### Weather lane

Jetstream events do carry identity (the acting account's DID — its
decentralized identifier — plus record identifiers). The classifier reads
each event transiently and emits only
symbols drawn from a **finite, enumerated metric alphabet** — 63 entries, no
free text. A DID cannot appear in the output because no DID is a member of
that set; the test suite asserts the containment against the whole fixture
corpus rather than merely promising it. The event itself never leaves the
classifier.

What the weather database persists: integer counts per (run, window, metric),
one observation-health row per window, and a resume cursor. That is all — no
raw events, DIDs, handles, record keys, CIDs, post text, or URLs.

### Social episode lane

An optional analysis layer detects aggregate *episodes* over the counters —
block bursts, like/repost storms, lulls — and, behind an opt-in edge sink,
concentration and overlap questions. Some of those questions are about *who
acted on whom*, which a counter has already discarded; so the edge sink,
**when explicitly enabled**, retains bounded actor→subject edge rows in a
**separate local database** with a short retention horizon. It is off by
default in code. The current deployment enables a narrow `block,listitem`
sink with 24-hour retention; those identity-bearing rows are local-only and
are never published.

What may be published is narrower still: aggregate-tier episodes only, and
only if the local edge store witnesses at least ten distinct actors for the
interval **and** — for a rate excess — no single actor could have produced
the whole departure. Times are coarsened to UTC hours; exact counts,
statistics, and stable IDs are omitted; missing evidence fails closed. This
is disclosure resistance, not anonymity. The full boundary, including what
each gate does *not* establish, is in
[`src/weatherwatch/social/BOUNDARIES.md`](src/weatherwatch/social/BOUNDARIES.md).

### PLC reduction

A separate offline command, `weatherwatch plc-reduce`, reduces a local copy
of the public `plc.directory` export — PLC is ATProto's identity directory,
and its export is identity-rich: DIDs, handles, service endpoints — into
bounded aggregate weekly facts (operation, creation,
tombstone, and endpoint-mutation counts). **Identity enters the reducer and
cannot leave the persisted output**: the output schema has no DID, handle,
endpoint, provider, pseudonym, or cohort representation.

The qualifications matter and are preserved exactly:

- per-fact low-count suppression (0–9 all become the same `UNKNOWN`) is
  qualified;
- compositional non-disclosure across multiple facts or publication revisions
  is **not** claimed;
- live PLC source acquisition and admission are **not** claimed;
- an endpoint mutation is not a migration, and a migration-like transition is
  not a *successful* migration.

The reducer contract, custody mechanics, and refused-claims list are in
[`docs/PLC-REDUCTION.md`](docs/PLC-REDUCTION.md).

## Observation limits

**One observer, not the network.** Weatherwatch watches one named Jetstream
endpoint. A paired probe on 2026-08-08 opened concurrent connections for the
same 120 seconds: `jetstream1.us-east` delivered **1.61×** the post volume of
`jetstream2.us-east`, while two sockets to the same endpoint agreed exactly
(self-control 1.000). Public Jetstream observers are not interchangeable
complete views. The deployed collector uses `jetstream1.us-east` — the
higher-observing side of that probe, which makes its numbers larger without
making them more global. Therefore:

- every number is "observed at this named endpoint during this interval" —
  never a network total, and higher observed volume is not evidence of
  greater completeness (there is no canonical denominator);
- streams from different observers cannot be summed — the read layer refuses
  to combine them, and changing endpoint starts a new run with a hard
  observation seam;
- comparisons *over time from one observer* are the defensible reading;
  absolute rates are not, in either direction.

Evidence: [`docs/JETSTREAM-OBSERVER-DIVERGENCE.md`](docs/JETSTREAM-OBSERVER-DIVERGENCE.md)
and the full measured-protocol record in
[`M0-VERIFICATION-RESULTS.md`](M0-VERIFICATION-RESULTS.md).

**Observed, unobserved, degraded — three different facts.** The instrument
distinguishes states that are easy to confuse and expensive to confuse:

| | |
|---|---|
| outside a run | **unobserved** — no row exists; nobody was watching, which is not quiet |
| inside a run, quiet | **observed, empty** — a row exists with `events_seen = 0` |
| inside a run, impaired | **conditioned** — `partial`, `gap`, or `degraded`, flagged on the window |

Nothing is interpolated across unobserved time, no baseline learns from a
window with a measured completeness defect, and a machine being off between
runs produces no fake missing windows. A quiet reading means observed quiet;
a gap says so.

## How it works

```text
Jetstream (one named endpoint)
   |
   v
classify()  — transient; emits only symbols from the finite 63-metric alphabet
   |
   v
in-memory counters -> 60-second windows closed by the stream's own clock
   |
   v
SQLite  — one transaction: counters + window health + resume cursor
   |
   v
weatherwatch report  ->  static HTML + JSON (published by directory swap)
```

Two optional side paths, both described above: the social edge sink taps the
same parsed message into a separate local store feeding episode detection,
and `plc-reduce` runs offline against a local export file. Neither touches
the weather lane's guarantees.

There is no HTTP server, no API, and no query surface in this codebase. The
collector may run continuously (supervised `systemd` units live in
[`deploy/`](deploy/README.md)), but it is an instrument that *may* run, not a
daemon that must: a bounded 30-minute run is a complete, valid observation.

## Try it

Requires Python 3.10+.

```bash
python -m pip install -e ".[dev]"

weatherwatch collect --duration 30m   # one bounded observation run
weatherwatch runs                     # runs and their coverage
weatherwatch stats                    # metric totals and rates
weatherwatch report --output ./report # static dashboard in ./report
```

Read commands default to the most recent compatible sequence of runs on one
endpoint; asking for an incompatible combination fails loudly rather than
producing a number that describes no actual observation. Keep the SQLite file
on local disk — never NFS/SMB.

Further commands (`series`, `ratios`, `correlate`, `status`, `social`,
`plc-reduce`, …) are documented in `weatherwatch --help` and
`weatherwatch <command> --help`.

Run the qualification suite with `./scripts/qualify.sh` — compile checks, the
fixture-privacy tripwire, and the full test suite, including end-to-end
collector tests against an in-process fake Jetstream.

## Deeper documentation

- What the public site publishes, and what a consumer may rely on:
  [`docs/PUBLIC-ARTIFACTS.md`](docs/PUBLIC-ARTIFACTS.md)
- Measured Jetstream protocol behaviour the design rests on:
  [`M0-VERIFICATION-RESULTS.md`](M0-VERIFICATION-RESULTS.md)
- The paired-observer divergence probe, standalone:
  [`docs/JETSTREAM-OBSERVER-DIVERGENCE.md`](docs/JETSTREAM-OBSERVER-DIVERGENCE.md)
- Social episode analysis — overview:
  [`src/weatherwatch/social/README.md`](src/weatherwatch/social/README.md);
  the privacy boundary:
  [`src/weatherwatch/social/BOUNDARIES.md`](src/weatherwatch/social/BOUNDARIES.md);
  the conditions/"social weather" layer:
  [`src/weatherwatch/social/field/README.md`](src/weatherwatch/social/field/README.md)
- PLC source reduction contract:
  [`docs/PLC-REDUCTION.md`](docs/PLC-REDUCTION.md)
- Time-only composition of already-reduced facts:
  [`docs/TEMPORAL-COMPOSITION.md`](docs/TEMPORAL-COMPOSITION.md)
- The local status and concern-visibility interface:
  [`docs/VISIBILITY.md`](docs/VISIBILITY.md)
- Deployment and publication:
  [`deploy/README.md`](deploy/README.md)
- Research candidates — filed, not authorized:
  [`docs/OBSERVATORY-ROADMAP.md`](docs/OBSERVATORY-ROADMAP.md) and
  [`docs/CANDIDATES.md`](docs/CANDIDATES.md)

## The beef thing

Yes, the beef. This project's dashboard originally lived at a joke path,
`/beef`, and entertained a hypothetical composite called the **Global Beef
Index**. The canonical site is now `weatherwatch.neutral.zone` and `/beef`
301s there, so old references keep working — but the joke stays, because the
joke is load-bearing:

**The joke is the disclaimer.** If a composite over these primitives is ever
defined, it stays named *Global Beef Index*, because a solemn name —
Behavioral Turbulence Index, Social Stress Index — would imply a validated
latent variable this instrument has not earned and cannot currently earn. No
such index exists; the dashboard shows a placeholder marked *calibration not
assumed*. (The need was demonstrated, not hypothetical: an LLM shown the old
`/beef` page confidently classified it as a social-drama detector, which is
why the negative scope now leads this document and the page.)

**The primitives are authoritative; composites are descriptive hints.** Any
future index would be a transparent composite over aggregate observable
behaviour, internally normalized against its own history. Nobody should have
to trust `BEEF = 73` without inspecting the aggregate series that produced
it; the primitive cards remain the receipts.

**The denominator gets a lawyer.** Every published ratio renders with its
numerator and denominator counts attached, window extremes are drawn only
from windows whose denominator clears a stated legibility floor, and excluded
windows are counted rather than dropped — because `block/follow = 22.75` off
a four-event denominator crops into a social index. A ratio can move because
the numerator rose, because the denominator fell, or because the denominator
got too small to mean anything. The ratio is the hint; the primitives are the
receipts.

**Narrative restraint.** "Post deletes elevated, blocks down" must not become
"morning-after beef" or "users are deleting evidence". Those readings may be
funny and even right, but the telemetry does not entail them. Humans may make
the joke; the instrument must not certify it.

## License

Licensed under either Apache-2.0 or MIT, at your option. See
[`LICENSE-APACHE`](LICENSE-APACHE) and [`LICENSE-MIT`](LICENSE-MIT).
