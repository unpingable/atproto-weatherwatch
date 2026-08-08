# M0 Verification Results

Spike run: **2026-08-08**, 11:56–12:15 EDT (15:56–16:15 UTC), Saturday.
Primary endpoint: `wss://jetstream2.us-east.bsky.network/subscribe`.
Scope: measurement + fixture generation only. No service was built.

Raw measurements: `measurements/*.json`. All aggregate; no identity retained.
Candidate under test: `../CANDIDATE-AGGREGATE-WEATHER-TELEMETRY.md` §E items 1–9.

> **One finding requires an operator decision before M1** — cross-instance
> divergence (§Item 11). It does not break the v0 architecture, but it changes
> what the service can honestly claim. See *Decisions required* at the end.

---

## Method

Unfiltered 10-minute survey (`m0_probe.py survey`), plus targeted probes for
cursor semantics, retention horizon, slow-consumer behaviour, and cross-instance
comparison. Total **198,250 events** inspected in the survey, plus ~40,000 more
across the control probes.

**Privacy procedure (changed from the candidate's original plan).** The
candidate proposed committing a raw JSONL corpus. That was replaced with:

```
live raw event -> inspect structure in memory -> scrubbed fixture -> discard raw
```

No raw event was written to disk at any point. `spike/scrub.py` is
deny-by-default: a key not explicitly known to be structural is dropped, and
every drop is counted so the drop histogram can be reviewed (it was — see
`measurements/survey.json:scrubber_dropped_keys`, which shows `text`, `alt`,
`description`, `displayName`, `pronouns`, `tag`, and ~180 third-party lexicon
fields all correctly discarded).

`spike/check_fixture_privacy.py` self-tests against 9 known-bad patterns before
validating, so a validator that always passes fails loudly instead. Current
state: **344 fixture lines clean, exit 0**.

---

## E. Aggregate measurements

### Stream rate and volume — jetstream2.us-east, unfiltered, 600.0 s

| Measure | Value |
|---|---|
| Messages | 198,250 |
| Mean events/sec | **330.4** |
| events/sec p50 / p95 / max | 334 / 392 / 488 |
| Approx bytes/sec | 185,203 (≈ 16 GB/day ingress) |
| Approx bytes/event | 561 |
| Parse failures | **0** |
| Messages lacking `time_us` | **0** |
| Unknown `kind` values | **0** |
| Unknown `commit.operation` values | **0** |
| Distinct collections observed | **71** |
| Reconnects during survey | 1 (unplanned, see below) |

Driftwatch's documented ~100 eps is for a `post,repost`-filtered subscription.
Unfiltered is **~3.3×** that — not the order-of-magnitude jump the candidate
warned might appear. Comfortably within one process.

### Event kinds and operations

| kind | count | | operation | count |
|---|---|---|---|---|
| `commit` | 197,926 | | `create` | 191,043 |
| `account` | 179 | | `delete` | 5,830 |
| `identity` | 145 | | `update` | 1,053 |

Envelope keys observed, exhaustively: `did`, `kind`, `time_us` (198,250 each,
i.e. always present), plus exactly one of `commit` / `identity` / `account`.

Commit keys: `collection`, `operation`, `rev`, `rkey` present on all 197,926
commits. `cid` and `record` present on exactly 192,096 = 191,043 creates +
1,053 updates. **`cid` and `record` are present iff the operation is not a
delete** — exact, no exceptions in 197,926 commits.

### Per-collection rates (derived, 600 s window)

| Collection | create | delete | update | delete/create |
|---|---|---|---|---|
| `app.bsky.feed.like` | 128,297 | 1,573 | — | 0.0123 |
| `app.bsky.feed.post` | 22,590 | 1,366 | 21 | 0.0605 |
| `app.bsky.feed.repost` | 20,418 | 654 | — | 0.0320 |
| `app.bsky.graph.follow` | 14,173 | 2,040 | — | **0.1439** |
| `app.bsky.graph.block` | 2,213 | 98 | — | 0.0443 |
| `app.bsky.feed.threadgate` | 1,401 | — | 8 | — |
| `app.bsky.feed.postgate` | 733 | — | — | — |
| `app.bsky.actor.profile` | 129 | — | 502 | — |
| `app.bsky.graph.listitem` | 197 | 30 | — | 0.152 |
| `app.bsky.actor.status` | 45 | 48 | 59 | 1.07 |

Headline rates: likes ≈ **214/s**, posts ≈ **37.7/s**, reposts ≈ **34/s**,
follows ≈ **23.6/s**, blocks ≈ **3.7/s**.

### Post structure (22,611 post records)

| Feature | count | fraction |
|---|---|---|
| with `reply` | 10,866 | **0.481** |
| with `embed` | 8,695 | 0.385 |
| with `facets` | 5,317 | 0.235 |
| quote (`embed.record`) | 1,323 | 0.0585 |
| quote+media (`embed.recordWithMedia`) | 213 | 0.0094 |
| **quote total** | **1,536** | **0.0679** |

Embed `$type` histogram: `images` 3,518 · `external` 3,155 · `record` 1,323 ·
`video` 438 · `recordWithMedia` 213 · **`gallery` 48**.

Facet feature `$type`: `#tag` 7,902 · `#link` 3,460 · `#mention` 795.

### Time and lag

`time_us` monotonicity across the whole run: **198,249 strictly increasing, 0
equal, 0 decreasing**, max backward jump 0 µs — *including across the
reconnect*. `time_us` is a strict total order with no observed duplicates.

Observed lag (`wall_clock − time_us`): p50 **−0.002 s**, p95 0.008 s, max 0.12 s,
min −0.008 s. Sub-100ms end-to-end. The negative median is local clock skew
(this host runs ~2 ms ahead of the relay), which matters: **a lag metric must
clamp negatives to zero**, exactly as `driftwatch/platform_health.py:118` does.

---

## Verdicts: candidate §E items 1–9

### 1. Unfiltered event rate — **VERIFIED**

330.4 eps mean, p95 392, max 488 on jetstream2.us-east; 185 KB/s.
Concurrent 45 s control measured 304.9 eps unfiltered.

*Implication*: single-process, in-memory counters are amply sufficient. At ~330
eps the per-event budget is ~3 ms before anything queues; classification is a
dict lookup. **This confirms the candidate's §C decision to drop driftwatch's
queue and writer thread.** Bandwidth (~16 GB/day) is the only real cost, and is
the one argument for the `compress=true` option (§13, still unresolved).

### 2. Omitting `wantedCollections` yields the full stream — **VERIFIED**

Concurrent 45 s windows: unfiltered connection saw **1,621** `app.bsky.feed.post`
events; a simultaneous post-only filtered connection saw **1,623**. Ratio
**0.999**. The unfiltered stream carries the full post stream plus 70 other
collections; it is a superset, not a sampled view.

Jetstream also accepted the subscription with no `wantedCollections` parameter
at all. 71 distinct collections appeared in 10 minutes, with a long third-party
tail (`at.podping.records.podping`, `dev.sensorthings.observationBatch`,
`fm.teal.alpha.feed.play`, `at.adsb.flight.record`, …).

*Implication*: the `unclassified` canary bucket in §D is not optional
bookkeeping — ~2% of commits are non-`app.bsky.*` lexicons and that share will
drift. It is the schema-drift detector.

### 3. Cursor retention horizon — **PARTIALLY VERIFIED**

| Requested lookback | Result |
|---|---|
| 1 min | served, first event exactly at requested cursor (Δ 0.0 s) |
| 10 min | served, Δ 0.0 s |
| 1 hour | served, Δ 0.0 s |
| 6 hours | **no event within 30 s timeout** |
| 24 hours | no event within 30 s timeout |
| 72 hours | no event within 30 s timeout |

**Established: at least 1 hour of replay is available, served precisely from the
requested cursor.** Beyond 6 hours, nothing arrived within 30 s.

**Not established**: whether ≥6 h is a *rejection* or merely a slow seek that
needs longer than 30 s. The probe cannot distinguish these. Do not quote a
horizon figure beyond "≥1 h verified."

*Implication*: crash recovery within the last hour can replay exactly. Longer
outages must be treated as gaps. This supports §D's `window_health.gap_us` and
argues against building any replay/backfill subsystem.

### 4. Delete commit shape — **VERIFIED, decisively**

5,830 delete commits observed:

| Property | count |
|---|---|
| carries `record` key | **0** |
| missing `collection` | **0** |
| missing `rkey` | **0** |
| carries `cid` | **0** |
| carries `rev` | 5,830 |

A delete commit is exactly `{collection, operation, rev, rkey}`. **Collection is
always present, so per-collection delete counting works. The record is never
present, so the deleted record's *shape* is unrecoverable.**

*Implication*: confirms the candidate's §Q6 requirement cut. "Replies
deleted/sec" and "quotes deleted/sec" are **not observable** without an
identity-keyed store of every post. `delete/create` per collection is fully
observable. The cut stands; no identity is needed.

A negative fixture (`synthetic|delete_with_record`) encodes the inverse case so
the classifier never comes to depend on record-absence as its delete test.

### 5. `kind` enum — **VERIFIED**

Exactly `commit`, `identity`, `account`. Zero unknown values in 198,250 events.

### 6. `commit.operation` enum, and whether `update` fires for feed records — **VERIFIED**

Exactly `create`, `delete`, `update`. Zero unknown values.

`update` is not confined to profile/service records: **`app.bsky.feed.post|update`
= 21** occurrences (~0.09% of post creates). Also observed on
`app.bsky.feed.threadgate` (8), `app.bsky.actor.status` (59),
`app.bsky.labeler.service` (8), and several third-party lexicons.
`app.bsky.actor.profile` is update-dominant (502 updates vs 129 creates).

*Implication*: the metric family must treat `post.update` as its own metric key,
not fold it into create. It is rare but real, and folding it in would silently
inflate the create rate.

### 7. Quote-post detection and `$type` reliability — **VERIFIED**

`$type` was present on **8,695 / 8,695** post embeds, **213 / 213**
`recordWithMedia.media` objects, and **12,157 / 12,157** facet features.
**Zero missing in every position checked.** `$type`-based quote classification
is safe.

Both expected shapes occur: `app.bsky.embed.record` (1,323) and
`app.bsky.embed.recordWithMedia` (213).

**New finding not in the candidate: `app.bsky.embed.gallery`** — 48 occurrences
as a post embed and 1 as `recordWithMedia.media`. Not in the candidate's assumed
embed vocabulary. It is media, not a quote, so it does not affect quote counting,
but the classifier's embed-type table must be an open enum with an
`embed.other` bucket rather than a closed match.

Reliability caveat kept honest: 100% presence over 10 minutes is strong evidence,
not a protocol guarantee. The `synthetic|embed_missing_type` fixture keeps the
missing-`$type` path exercised.

### 8. Exact NSIDs for the v0 metric family — **VERIFIED (all 7 present)**

| NSID | events in 600 s |
|---|---|
| `app.bsky.feed.like` | 129,870 |
| `app.bsky.feed.post` | 23,977 |
| `app.bsky.feed.repost` | 21,072 |
| `app.bsky.graph.follow` | 16,213 |
| `app.bsky.graph.block` | 2,311 |
| `app.bsky.actor.profile` | 631 |
| `app.bsky.graph.listitem` | 227 |

Also worth adding to v0, observed at usable volume and cheap:
`app.bsky.feed.threadgate` (1,409), `app.bsky.feed.postgate` (735),
`app.bsky.actor.status` (152), `app.bsky.graph.listblock` (12),
`app.bsky.graph.list` (5), `app.bsky.labeler.service` (8).

### 9. `identity` / `account` event shapes — **PARTIALLY VERIFIED, one falsification**

`account` events (179 observed) — **VERIFIED**:
keys `did`, `seq`, `time`, `active` on all 179; `status` on exactly 48.
`active=true` 131, `active=false` 48. **`status` appears iff `active=false`** —
exact correspondence, 48/48.
Observed statuses: `deleted` 19, `takendown` 15, `deactivated` 14.
**`suspended` was NOT observed** — it is in the protocol vocabulary but absent
from this sample. Treat the status set as open.

`identity` events (145 observed) — **`handle` FALSIFIED**:
keys are `did`, `seq`, `time` on **145/145**. **`handle` was present on zero
identity events.**

This contradicts `driftwatch/src/labeler/identity.py:70`, which reads
`identity.get("handle", MISSING)` as a primary field. Driftwatch's MISSING
sentinel means it degrades correctly rather than corrupting, but any design that
*expects* handles from Jetstream identity events is wrong on this evidence.

Cannot distinguish "Jetstream never forwards handle" from "handle is only sent
when it changed and no handle changed in this 10-minute window." 145 events is
a small sample. **Recorded as falsified-for-planning, sample-limited.**

*Implication for this project*: none, and that is the point — the candidate
already refuses handles. This finding removes the last argument anyone might
make for resolving them.

---

## CURSOR BOUNDARY SEMANTICS — **VERIFIED, unanimous**

The candidate's §Q6 refuses identity-based deduplication, so the exact boundary
behaviour of `cursor=T` decides whether "resume exact" produces overlap, a gap,
or neither. Two probes, 12 trials total, both unanimous.

### Probe 1 — is `cursor=T` inclusive or exclusive? (6/6 trials)

Procedure per trial: connect fresh, collect 60 post events, take `T = time_us`
of event #40, close cleanly, reconnect with `cursor=T`, inspect the first event
returned. Identity comparison was done on a transient in-memory key
`(kind, did, collection, rkey, rev, operation)`; only the verdict was recorded.

**Result: `inclusive_replays_T`, 6/6.** In every trial the first event returned
was *the event at T itself* — `first_resumed_minus_T_us = 0` and an exact
identity match, not merely a timestamp match. The next in-stream event was
14,772–73,198 µs later, so the match is unambiguous.

> **`cursor=T` is inclusive. Resuming from the last-seen `time_us` replays that
> event.** Naive "resume exact" produces a one-event overlap, i.e. silent
> double-counting — precisely what the no-dedup design cannot absorb.

### Probe 2 — does `cursor=T+1` resume exactly? (6/6 trials)

**Result: `exact_no_overlap_no_gap`, 6/6.** The first event returned was exactly
the event that followed T in-stream; the event at T was not replayed and nothing
was skipped.

This is sound because `time_us` is a strict total order with no duplicates
(198,249/198,249 strictly increasing, zero ties). `T+1` therefore excludes
exactly the event at T and nothing else.

### What this means for the architecture

The candidate's §Q6 recommendation — *"resume from the exact last cursor and
accept a possible small gap, then flag the window with `resume_boundary=1`"* —
was written against an unverified assumption and is **more pessimistic than
reality**. The measured behaviour supports a strictly better option:

- **Persist `last_seen_time_us`; resume with `cursor = last_seen + 1`.** Zero
  overlap, zero gap, zero identity retained, for clean reconnects within the
  replay horizon.
- Combined with the ≥1 h replay horizon (Item 3) and the idempotent bucket
  rewrite pattern (§B, from `facts_export.py:120-146`), a crash can resume from
  the **last flushed** cursor and *recount the interrupted window correctly*,
  rather than marking it degraded.

**This is not being applied.** It changes §Q6's design conclusion, and per the
M0 brief live evidence that changes the design materially gets reported, not
quietly adopted. See *Decisions required*.

`resume_boundary` / `gap_us` remain necessary regardless: they cover outages
longer than the replay horizon, and unclean shutdowns where the cursor itself
is stale.

---

## Item 5 extras

### Slow-consumer behaviour — **VERIFIED for ≤75 s: backpressure, not loss**

Connected, read one event (lag 0.02 s), then stopped reading for 75 s with a
4-message client queue, then read again.

| Measure | Value |
|---|---|
| Connection closed during stall | **no** |
| Close code | none |
| First event after stall, lag | **75.09 s** |
| Interpretation | `backpressure_buffered_consumer_falls_behind` |

**A slow consumer falls behind visibly rather than losing events silently.** The
post-stall lag matched the stall duration to within 0.1 s — we resumed exactly
where we left off, 75 s in arrears.

*Implication*: this is the best possible answer for the honesty ledger. Consumer
slowness manifests as **rising `stream_lag_s`**, which `platform_health.py`
already tracks and gates on (`LAG_HIGH_THRESHOLD_S`). It is a detectable,
recoverable, non-silent failure mode.

**Not established**: behaviour beyond 75 s. A server-side buffer limit or idle
timeout may exist further out. Untested.

### Cross-instance completeness — **FALSIFIED (as an assumption of completeness)**

This is the finding that needs an operator decision.

Four concurrent 120 s connections, all filtered to `app.bsky.feed.post`, all
from this host, same wall-clock window:

| Connection | posts in 120 s | eps |
|---|---|---|
| jetstream2.us-east **#A** | **4,400** | 37.3 |
| jetstream2.us-east **#B** | **4,400** | 37.3 |
| jetstream1.us-east | **7,088** | 59.7 |
| jetstream1.us-west | **6,928** | 58.3 |

| Ratio | Value |
|---|---|
| same instance, A/B (self-control) | **1.000** |
| jetstream1.us-east / jetstream2.us-east | **1.611** |
| jetstream1.us-west / jetstream2.us-east | **1.575** |

**Two independent sockets to the same instance agreed exactly** — identical
counts and identical `span_us` to the microsecond. So the divergence is
server-side, not an artefact of running several sockets from one host, not local
contention, and not measurement noise.

**jetstream2.us-east delivered ~62% of the post volume that jetstream1.\*
delivered in the same window.** This reproduced across probes: the 45 s control
run showed 37.7 eps (j2) vs 48.4 / 48.6 eps (j1), and the 10-minute survey's
39.96 posts/s is consistent with the j2 figure.

**What this does NOT establish.** Aggregate counts cannot prove set
inclusion — the higher-volume instance may be a superset, the two may partially
overlap, or j2 may have been degraded during this hour. Proving it would require
comparing per-event identity across instances, which this project refuses to
retain. That refusal is correct and should not be relaxed for this.

*Implication*: the candidate's §E item 11 assumed a single-instance limitation
would be "documented, not a reason to build multi-instance reconciliation." That
stance survives — but the limitation is now **measured and large (≈38% volume
difference)**, not hypothetical. Every rate this service publishes is a rate
*as observed at one relay*, and on this evidence that qualifier is
load-bearing, not boilerplate.

### Compression (`compress=true`) — **UNRESOLVED**

Not tested. At 185 KB/s (~16 GB/day) it is a real but non-blocking cost.

---

## Live demonstration: the survey lost events and cannot say how many

At t≈480 s the survey's connection dropped:

```
[survey] connection error (ConnectionClosedError: no close frame received or sent); retry in 3s
[survey] connected (1 reconnects so far)
```

The spike reconnects at live head with **no cursor** (it is a survey, not a
collector). Consequences, from the measurements:

- Mean rate over the full run: **330.4 eps**, against a pre-reconnect steady
  state of **338–339 eps** across four consecutive 60 s windows.
- Extrapolating the steady rate over 600 s predicts ~203,400 events; 198,250
  were observed. **Shortfall ≈ 5,000 events, ≈ 2.5% of the run.**
- One 1-second bucket recorded 7 events (`eps_min`), the reconnect seam.
- `time_us` monotonicity was *preserved* across the gap — 0 decreasing. **A
  monotonic cursor is not evidence of completeness.**

That figure is an inference from rate extrapolation, not a count. The survey has
no way to produce a count, because it discarded the cursor. That is exactly the
failure the candidate's `window_health` table exists to make visible, observed
in the wild within ten minutes of first contact — and it is the concrete
argument for `cursor = last_seen + 1` resume in the real collector.

---

## Fixture corpus

| File | Lines | Contents |
|---|---|---|
| `fixtures/jetstream_shapes.jsonl` | 307 | Live-derived, scrubbed. 194 distinct structural signatures. |
| `fixtures/jetstream_synthetic.jsonl` | 37 | Hand-written malformed, negative, and scar fixtures. |
| `fixtures/malformed_lines.txt` | 8 | Non-JSON frames for the parse-failure path. |

Live fixtures carry a `_shape` signature and, on commits, a
`_record_key_present` boolean — the delete-classification fact that is otherwise
lost when a key is simply absent from the scrubbed output.

Synthetic fixtures deliberately cover what live capture cannot or should not:
empty envelope, unknown `kind`, `null` kind, missing `time_us`, `time_us` as a
string, negative `time_us`, unknown operation, absent collection, `commit` as a
scalar, `record` null / list, **delete carrying a record** (never observed —
negative fixture), embed missing `$type`, unknown embed `$type`, `recordWithMedia`
missing media, partial reply, reply-that-is-also-a-quote, unknown collection,
identity with and without `handle`, account missing `active`, account with
unknown status, and three **event-time-hygiene scar fixtures** — `createdAt` in
year 2999, year 1970, and unparseable garbage — encoding
`driftwatch/specs/gaps/gap-spec-event-time-hygiene.md` directly as a test.

Validation: `python3 spike/check_fixture_privacy.py` → **344 lines clean, exit 0**.
It rejects `did:plc:` / `did:web:` / `did:key:`, any non-`did:example:` method,
Bluesky-shaped handles, hostnames outside reserved TLDs, `at://` URIs with
non-synthetic authorities, the forbidden key set (`text`, `alt`, `title`,
`description`, `displayName`, `tag`, `tags`, `name`, `note`, `comment`, `bio`,
`email`, `nickname`), and any string over 96 chars.

---

## Summary of verdicts

| # | Assumption | Verdict |
|---|---|---|
| 1 | Unfiltered event rate | **VERIFIED** — 330 eps, 185 KB/s |
| 2 | Omitting `wantedCollections` gives full stream | **VERIFIED** — ratio 0.999, 71 collections |
| 3 | Cursor retention horizon | **PARTIALLY VERIFIED** — ≥1 h exact; ≥6 h no response in 30 s |
| 4 | Delete commit shape | **VERIFIED** — 5,830/5,830, no record, collection always present |
| 5 | `kind` enum | **VERIFIED** — commit/identity/account only |
| 6 | `operation` enum + `update` on feed records | **VERIFIED** — create/delete/update; post updates real but rare |
| 7 | Quote embeds + `$type` reliability | **VERIFIED** — 100% `$type` presence; new `embed.gallery` type found |
| 8 | The 7 v0 NSIDs | **VERIFIED** — all present at usable volume |
| 9 | identity/account shapes | **PARTIALLY VERIFIED** — account exact; **identity `handle` FALSIFIED (0/145)** |
| — | **CURSOR BOUNDARY SEMANTICS** | **VERIFIED** — `cursor=T` inclusive (6/6); `cursor=T+1` exact (6/6) |
| 10 | `time_us` monotonic | **VERIFIED** — 198,249/198,249 strictly increasing, no ties |
| 11 | One instance = complete view | **FALSIFIED as an assumption** — j1/j2 ratio 1.61, self-control 1.000 |
| 12 | Slow-consumer behaviour | **VERIFIED ≤75 s** — backpressure, lag 75.09 s, no loss, no disconnect |
| 13 | `compress=true` worth it | **UNRESOLVED** — not tested |

---

## Unresolved

1. **Cursor horizon beyond 1 h.** Rejection vs slow seek, undistinguished.
2. **Cross-instance divergence: cause.** Is jetstream1 a superset, is j2
   sampling, or was j2 degraded that hour? Not answerable without either a
   longer multi-hour comparison or per-event identity comparison (refused).
3. **Slow-consumer behaviour beyond 75 s.** Server-side buffer/idle limits.
4. **Whether Jetstream ever forwards `identity.handle`.** 0/145 here; sample
   too small and window too short to be conclusive.
5. **Whether `account.status = suspended` occurs.** Not observed in 179 events.
6. **`compress=true`** — untested.
7. **On-disk SQLite size for the corrected row counts** (§D of the candidate) —
   arithmetic corrected, file size still unmeasured. Measure at M3.

---

## Decisions required before M1

1. **Cursor resume policy.** Evidence supports `cursor = last_seen_time_us + 1`
   for exact, overlap-free, gap-free resume — strictly better than the
   candidate's "accept a gap and flag the seam." Adopt it, or keep the
   pessimistic §Q6 design? *(Recommendation: adopt; keep `gap_us` /
   `resume_boundary` for outages beyond the replay horizon.)*

2. **Which relay, and what the service claims.** jetstream2.us-east — the
   endpoint both driftwatch and labelwatch default to — delivered ~62% of
   jetstream1's post volume. Options: switch to jetstream1, run two instances
   and publish both series, or stay on one and label every rate "as observed at
   `<instance>`." *(No recommendation. This is a claims decision, not a
   technical one, and it should not be made silently.)*

3. **Metric family additions** surfaced by measurement: `post.update` as its own
   key; `embed.other` as an open-enum bucket (`app.bsky.embed.gallery` already
   exists and was not in the candidate); threadgate/postgate/actor.status as
   cheap additions.
