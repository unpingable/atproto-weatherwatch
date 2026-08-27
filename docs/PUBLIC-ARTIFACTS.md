# The public artifact contract

What `weatherwatch.neutral.zone` publishes, what a consumer may rely on, and
which of those guarantees exist to protect the reader rather than the server.

Status: current as of 2026-08-26. Inherits every boundary in
[`../src/weatherwatch/social/BOUNDARIES.md`](../src/weatherwatch/social/BOUNDARIES.md)
and the field decisions in
[`../src/weatherwatch/social/field/DECISIONS.md`](../src/weatherwatch/social/field/DECISIONS.md).

---

## 1. What is published

| artifact | bounded? | schema |
|---|---|---|
| `index.html` | yes — charts collapse to a fixed column budget | — |
| `summary.json` | **yes — recent windows only** | `weatherwatch.summary/v2` |
| `history/index.json` | grows one row per observed day | `weatherwatch.history-index/v1` |
| `history/YYYY-MM-DD.json` | one UTC day each | `weatherwatch.history-day/v1` |
| `social.json` | yes — disclosure-gated episodes | `weatherwatch.social/v3` |
| `findings/index.json` | yes — one row per published finding | `weatherwatch.findings.index/v1` |
| `findings/<slug>/index.html` | yes — permanent finding page | — |
| `findings/<slug>/finding.json` | yes — stable claim, design and limits | `weatherwatch.finding/v1` |
| `findings/<slug>/receipts/*.json` | yes — aggregate receipts only | finding-specific, versioned |
| `og-card.png` | static | — |

Finding records are historical publications, not current observations. Report
generation does not rewrite their publication date or silently refresh their
qualification. The current observation remains in `summary.json`; a consumer
must not substitute a published finding for current acquisition state.

## 2. The bounded-summary policy

`summary.json` listed every window ever observed. On the live estate that was
**4.1 MB and 24,609 windows**, growing by roughly 2,000 windows a day forever,
and every visitor downloaded all of it to render a page that reads the last
few.

It now carries **the most recent 24 hours of windows, capped at 2,000**
(`archive.RECENT_SECONDS`, `archive.RECENT_CAP`). The bound is expressed in
seconds so it means the same thing at any bucket width, with the count cap so a
narrow bucket cannot reinflate the artifact. Measured on a 30-day synthetic
estate: **6,878,820 → 239,695 bytes**.

Every other field is unchanged and was already bounded. `interval` still
describes the **whole** observed span, not the retained tail — a consumer
reading it learns the true extent even though `windows` no longer covers it.

## 3. How historical retrieval works

```
GET /summary.json          -> .history.index  == "history/index.json"
GET /history/index.json    -> .days[] = {date, path, window_count, digest, bytes}
GET /history/2026-08-14.json -> {date, windows: [...], window_count}
```

Fetch the index, pick the days you need, fetch those. There is no query API and
no database behind any of this: they are static files, so a cache, a mirror or
a `curl` loop all work, and nothing about a visitor's interest is observable
beyond ordinary web-server logs.

**Partition:** UTC calendar day. Coarse enough to bound a fetch, and already
the unit `interval` discloses.

**Deterministic, not immutable.** The same windows always produce the same
bytes — sorted keys, fixed separators, and deliberately **no `generated_at`
inside a day file**, so an unchanged day is not rewritten and its `mtime` does
not churn on a five-minute publish cadence. A *closed* day can still change:
the collector accepts late events and a window can be re-observed by a later
run. Rather than promise immutability the pipeline cannot keep, each day
carries a `digest` and the index republishes it, so a change is detectable.

**A view, not a vault.** The rendered tree is rebuilt and swapped atomically on
every publish, so the archive's extent is the *observation store's* extent. If
windows leave the store, their day files stop being written and the index stops
listing them. This is stated rather than hidden, and it is deliberately not
solved here: making published history outlive the store is a retention decision
about public artifacts, and retention is owned separately. What is guaranteed
and tested is narrower — the recent/archive split loses nothing, and any
shrinkage is visible in `day_count` and `first_archived_day`.

**Absent or corrupt.** The index lists only days written *and read back* in the
run that produced it. A day that failed to serialise is recorded in `problems`
rather than silently omitted. A date absent from the index has no artifact:
expect `404`, and report it as unavailable — never infer a quiet period from a
missing file. That is the same posture the rest of the instrument takes toward
unobserved time.

## 4. Compatibility

`windows` keeps its name, its position, and its element shape. A consumer that
reads it for recent state keeps working unchanged. What broke is the
*assumption that it is complete*, and the artifact now says so in a field
rather than in prose:

```json
"history": { "windows_are_recent_only": true, "index": "history/index.json", ... }
```

`summary.json` also gained a `schema` key. It had none, which left a consumer no
way to detect any change at all. Treat its absence as v1.

A consumer that needs the full span should read `history.index` and page
through the days. A consumer that only needs current conditions should read
`conditions` and `freshness` and ignore `windows` entirely.

## 5. Precision and privacy invariants

The split **moves published data; it does not sharpen it.** These are asserted
in `tests/test_archive.py`, not merely intended:

- A day file carries exactly the five fields a window has always published —
  `bucket_start`, `quality`, `flags`, `events_seen`, `observed_duration_us` —
  and no others. No per-metric breakdown per window; that would be new
  precision, not a new location.
- `bucket_start` remains a whole-second bucket boundary. No sub-second or
  per-event timing appears anywhere.
- No identity-shaped value reaches any archive artifact.
- **Filenames disclose only a date.** A filename is metadata that caching
  proxies and access logs retain, so it carries no run id, no endpoint, and
  nothing the `interval` block did not already publish.
- Unobserved windows appear in the archive *marked unobserved*. They are not
  absences and they are not zero.

## 6. Mobile behaviour

The page is one responsive document; there is no separate mobile UI and no
user-agent branching. Expectations, verified at 320 / 375 / 390 / 430 px and at
768 / 1280:

- **No horizontal page scrolling at any width.** Dense tables scroll *locally*
  and carry a shadow affordance that appears only on the side with more
  content.
- **No text below 11px**, and no interactive target below 44px.
- The **state ladder** lays out as a grid on narrow screens rather than
  wrapping into a ragged row with the last state orphaned.
- **State names never break mid-word.** A published state label rendered as
  "Turbul / ent" reads as a rendering fault, so the history strip opts out of
  the page's `overflow-wrap:anywhere`.
- The **ratio table stacks** into labelled blocks below 700px instead of
  scrolling to 1782px. Stacking is taller; a five-screen-wide table is not
  legible at any height. Numerator, denominator and value stay physically
  together, which is the one thing the ratio presentation must never lose.

Desktop presentation is unchanged: every mobile rule is inside a `max-width`
media query, and the narrow-viewport block sits **last** in the stylesheet so
it can actually win against the desktop rules it overrides.
