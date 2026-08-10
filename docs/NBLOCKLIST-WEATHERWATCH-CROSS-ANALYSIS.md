# nblocklist × Weatherwatch cross-analysis

**Status: blocked at Phase 1. No analysis was run.**
Investigated 2026-08-10. Read-only throughout; no production behaviour changed.

Two independent blockers, either of which alone would stop this:

1. **`nblocklist` does not exist** on any machine reachable from here.
2. **Weatherwatch holds 28.1 hours of history**, which is far too little for
   the lag, event-window and control design this asks for — even with a
   perfect moderation dataset in hand.

The second finding is the more useful one, because it is measurable and it
tells you when to come back.

---

## 1. Data sources and exact historical coverage

### nblocklist — not found

| where I looked | result |
|---|---|
| `/home/jbeck/git/**` (repo dirs, maxdepth 3) | no match for `*block*` except an unrelated governance doc |
| case-insensitive content grep for `nblocklist` across `*.md`, `*.py`, `*.toml`, `*.json` | **zero occurrences anywhere** |
| production host `/opt`, `/var/lib`, all systemd units | absent |
| `mac` host `~/git` | absent |

The nearest block-adjacent project is `nebgraph` — *"local-only tool for
inspecting observed ATProto block-edge topology inside a user-supplied mutual
cohort"* — which is an identity-bearing graph inspector over a hand-supplied
cohort. That is close to the opposite of what this analysis needs, and joining
it would breach the privacy boundary rather than respect it.

So Phase 1 questions 1–7 have no answer to give. I did not infer a schema, and
I did not reconstruct history from snapshots.

**If nblocklist exists somewhere I cannot see** — another machine, a hosted
service, a private repo — the rest of this document still applies, because
blocker 2 is independent of it.

### Weatherwatch — real but shallow

| | |
|---|---|
| first window | 2026-08-08 20:15:00Z |
| last window | 2026-08-10 00:29:00Z |
| span | **28.2 hours** |
| windows | 1,710 |
| eligible for analysis | **1,687 (98.7%)** |
| partial | 17 · gapped 0 · truly degraded 0 · warming 12 |
| resolution | 60 s |
| source | `wss://jetstream1.us-east.bsky.network/subscribe` |

Data *quality* is excellent. Depth is the problem. Continuous collection only
started 2026-08-09; everything before that is bounded manual sessions.

### An alternative source that does exist

`labelwatch.label_events` on the same host spans **2026-02-24 → 2026-08-10**
(~5.5 months) of labeler moderation activity with per-event timestamps and
`labeler_did`. Aggregated to minute buckets with the DID dropped, it is
structurally the series this analysis wants, and it is already colocated with
Weatherwatch.

It does not rescue the analysis today — the overlap with Weatherwatch is still
28 hours — but it means the design is *viable later without building a new
collector*. Flagged, not built.

---

## 2. Clock and time semantics

Weatherwatch windows are keyed on Jetstream `time_us`, the relay-observed
clock, never `record.createdAt` (producer-controlled, and known to carry
year-2999 values). `window_health.observed_from_us` / `observed_to_us` are
event-derived. Run `started_at` / `ended_at` are host wall clock and are *not*
the data interval — a run resuming from an old cursor covers far more stream
time than it spent running.

`labelwatch.label_events.ts` would need its own custody check before use:
whether it is emission time, ingest time, or a labeler-claimed value materially
changes any lead/lag result. Not investigated, since the analysis is blocked.

---

## 3. Privacy boundary (design, unused)

The shape would have been:

```
moderation events -> minute buckets -> DROP subject identity
                                                    \
                                                     -> time-aligned join
                                                    /
Weatherwatch minute buckets ------------------------
```

Join key is **timestamp only**. No DID-to-DID join, no handle join, no actor
graph, no durable cross-system identity key, no account-level output. The
intermediate dataset would carry `bucket_start`, aggregate counts from each
side, and Weatherwatch health fields — nothing else.

Nothing was built, so nothing was exported and there is nothing to scan. The
existing Weatherwatch privacy posture is untouched (verified below).

---

## 4. Available nblocklist aggregate primitives

**None.** The system was not found.

---

## 5. Weatherwatch primitives that would have been used

All present and queryable at 60 s resolution: `post.create` (+ `.reply`,
`.quote`, `.embed.*`), `post.update`, `post.delete`, `like.create/.delete`,
`repost.create/.delete`, `follow.create/.delete`, `block.create`,
`block.delete`, `listitem.create/.delete`, `profile.create/.update`,
`account.event` (+ active/status), `identity.event`, `threadgate.*`,
`postgate.*`, `actor_status.*`, `unclassified.collection`, plus
`_events_total` from `window_health.events_seen`.

The taxonomy split has **not** landed as separate keys — `unclassified.collection`
still merges "untracked vocabulary" with a never-observed "missing collection"
failure. The dashboard presents them correctly; the persisted key does not
distinguish them. See `docs/CANDIDATES.md` C1.

---

## 6. Missing-data and health conditioning rules

These were established and would have been applied unchanged: unobserved
windows are `count=None`, never zero; rates divide by observed duration, not
nominal width; partial, gapped, lossy and coverage-degraded windows are
excluded from baselines; seams, `lagged` and `recovering` windows stay eligible
because replay reconstructs them completely. Runs from different endpoints or
overlapping intervals refuse to combine.

---

## 7–11. Correlations, lag, event windows, asymmetry, controls

**Not reached.** No moderation series to correlate against.

---

## 12. Why the sensor / actuator / shared-weather question stays open

Discriminating "moderation reacts to weather" from "moderation precedes
weather" from "both track a third process" requires many independent episodes
with clean lead/lag structure. With one day of data and no event series, the
data are compatible with all four hypotheses, which is the same as saying they
are uninformative. No preference is expressed.

---

## 13. Strong findings

**F1 — nblocklist is not present.** Verified across every repo, the production
host and the second machine. The string occurs in no file.

**F2 — Weatherwatch is far too shallow, and the reason is autocorrelation, not
row count.** Minute-to-minute platform activity is strongly self-correlated, so
1,687 eligible minutes carry far less independent information than they look
like they do. Measured lag-1 autocorrelation and AR(1) effective sample size:

| metric | N eligible | lag-1 *r* | **N_eff** | days to reach N_eff 2000 |
|---|---|---|---|---|
| `post.create.quote` | 1,687 | 0.903 | **86** | 27.1 |
| `block.create` | 1,687 | 0.830 | **157** | 14.9 |
| `block.delete` | 1,687 | 0.804 | **183** | 12.8 |
| `follow.delete` | 1,687 | 0.487 | 582 | 4.0 |
| `post.delete` | 1,687 | 0.483 | 588 | 4.0 |
| `listitem.create` | 1,687 | 0.387 | 745 | 3.1 |

A ±60 min sweep at 1-minute resolution tests **121 lags**. Against an effective
N of 86–183 for exactly the metrics this analysis cares about most — quotes and
blocks — a sweep like that is a fishing expedition. It would reliably produce a
confident-looking peak from noise, and nominal *p*-values would be wrong anyway
because the autocorrelation violates their independence assumption. This is the
"decorative significance test" failure mode the brief warns against, and here it
is quantified rather than asserted.

**F3 — Phase 8 controls are arithmetically impossible today.** Hour-of-week
matching needs 168 cells; 28 hours covers 28 of them with at most one sample
each. There is nothing to match against.

**F4 — Phase 5 has almost no room.** A T−30m…T+60m window is 90 minutes; 28
hours holds at most **18** non-overlapping windows, and only if qualifying
bursts happened to occur that often.

---

## 14. Weak findings

None. Nothing was measured that would qualify.

---

## 15. Null results

**None — and this distinction matters.** Nothing was tested, so nothing came
back negative. "No detectable aggregate coupling" would be a genuine and useful
result; this is not that. It is a Phase-1 stop, and it should not be recorded
or cited as evidence of absence.

---

## 16. What the data cannot establish

With 28 hours and no moderation series: any lead/lag ordering; any event-window
response shape; whether additions and removals differ; any regime sensitivity;
any amplification or decay measure; and any of the four framing hypotheses.

Diurnal structure alone is a confound that one day cannot separate — a single
pass through one day/night cycle cannot be distinguished from a response to
anything else that happens to follow the same rhythm.

---

## When this becomes worth re-running

1. **Continuous collection keeps running.** It is up now with a 5-minute
   publish timer. `analysis/power_check.py` re-computes the table above; re-run
   it rather than guessing.
2. **~2 weeks** makes a narrow lag analysis defensible on the
   faster-decorrelating primitives (`follow.delete`, `post.delete`,
   `listitem.create` reach N_eff 2000 in 3–4 days).
3. **~4 weeks** brings blocks and quotes into range.
4. **4–8 weeks** is the floor for hour-of-week controls with more than one
   sample per cell.
5. **A moderation event series with custody-checked timestamps** — either
   nblocklist if it exists elsewhere, or `labelwatch.label_events` aggregated
   to minute buckets with subject identity dropped at the aggregation step.

Narrow the lag sweep when the time comes. ±60 min at 1-minute resolution is 121
tests; a hypothesis-driven window (say ±15 min, or 5-minute resolution) spends
far less of the evidence budget.

---

## Interpretation discipline, for whoever picks this up

Temporal ordering is evidence about sequence, not causality. "X tends to
precede Y" is the strongest claim this design can ever support, and it stays
true even with months of data. Two systems on one platform share every
confounder there is — time of day, traffic volume, a single viral thread.

And the framing to avoid inheriting: this is anonymous epidemiology of posting,
not contact tracing. The moment a design needs to know *which* subject was
added to answer a question, the question has left this boundary.
