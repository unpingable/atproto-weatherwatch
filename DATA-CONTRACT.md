# Weatherwatch data contract

Weatherwatch is an observatory, not a system of record. It may lose history.
It must never claim continuity or coverage it cannot substantiate, and it must
never silently shrink the history it has already published.

> **Historical observation data is not required for service recovery unless an
> explicitly identified capability depends on it.**

No capability is currently identified as depending on it. A collector with an
empty store starts, observes, reports and publishes. Readings that need
history, such as baselines and departures, say their baseline is too short
until new history accrues, and nothing speaks for time before the store
began.

## What it observes

- **Jetstream.** One named endpoint per run. Each event is classified
  transiently into a fixed metric alphabet; the store keeps integer counts per
  (run, window, metric), one health row per window, one row per observation
  run, and a resume cursor per endpoint. No raw events and no identities.
- **PLC.** `weatherwatch plc-reduce` reduces a local copy of the sequenced
  `plc.directory` export into weekly aggregate facts. Each fact carries its
  event window and, separately, `acquired_at`: when the export was fetched.
  Nothing is written to the observation store.
- **Trends.** Not implemented. Nothing is observed.

## Three independent guarantees

**Retention.** The observation store keeps every window it has recorded; there
is no pruning. It is one local SQLite file with no replication or backup
provided by Weatherwatch. The optional social edge store is separate and keeps
identity-bearing rows for a short horizon (24 hours in the current deployment).
The published archive is regenerated from the observation store on every
publish, so on its own it lasts only as long as the store.

**Observation continuity.** Time is observed only inside a run. A window
inside a run is recorded even when empty; time outside a run is unobserved,
which is different from quiet. Windows and the cursor commit in one
transaction, so a restart resumes exactly where committed data ends. Each
store records a `store_id` and `store_created_at` when it is created, and
`weatherwatch status` and `summary.json` report them as "history begins here;
earlier history unknown". Status reports persistence continuity as PRESENT
only when the current run resumed from a persisted cursor; a cold start is a
fresh lineage and is reported UNKNOWN.

**Recovery.** What happens after loss depends on the source:

- *Jetstream: gap by default.* A new store has no cursor, so the collector
  takes the live tail and the lost interval stays unobserved. Replay happens
  only if an operator deliberately seeds the endpoint cursor in the new store
  before the first run (there is no dedicated command), and only within the
  relay's replay horizon. A replayed run records `resume_cursor_at_start`,
  shows `replayed_from_cursor: true` in `summary.json`, and marks its first
  window as a seam; a resume that lands past the requested cursor is recorded
  as a gap. The warm-up baseline checkpoint is bound to its endpoint and
  `store_id` and is not restored onto a different store, so a new store warms
  up from its own stream.
- *PLC: re-fetch and re-reduce.* A fresh export produces the facts again,
  stamped with the new acquisition time; event time and acquisition time stay
  separate.
- *Trends: none.*

## Publication continuity

The published site is stored apart from the observation store, so its
`history/index.json` is the record of what was published. Before each swap,
`deploy/publish.sh` runs `weatherwatch publish-continuity`, which compares the
candidate's archived days with the published ones. It refuses a candidate whose
history starts later, has fewer days, or drops a published day. It also
refuses when the published index cannot be read; that case is treated as
unknown, never as clean. An operator can publish anyway with
`--declare-new-epoch`. The page, `summary.json` and `history/index.json` then
carry a discontinuity notice ("history before X was lost", or "unknown" when
the earlier index could not be read), and later publishes carry the notice
forward.

## State whose loss matters

- **The observation store** (the SQLite database and its WAL). Losing it loses
  the windows, runs, cursors and store identity. Recovery is a new store with a
  new identity, and a gap.
- **The baseline checkpoint** beside the store. It only shortens warm-up;
  losing it costs a warm-up period and nothing else.
- **The published site.** Its history index is the continuity record for
  publication. If it is lost, continuity is unknown and publishing needs a
  declared epoch.
- **The social edge store.** Losing it makes episode evidence for its horizon
  unavailable, and the social section fails closed. It does not affect the
  weather counts.

## What it never claims

- That any relay, or the union of relays, is complete. Every figure is "as
  observed at this endpoint".
- That anything is known about time before `history_begins_at`, or that an
  unobserved interval or an unlisted day was quiet.
- That a recreated store continues an earlier one, or that structural
  coherence alone shows continuity.
- That replayed data was observed live.
- That published day files are immutable. They are deterministic and carry
  digests.

## Known limitations

- In a replayed run only the first window is marked; later replayed windows
  are distinguishable only by the run's replay flag and by measured lag.
- The publish guard compares archived days, not the most recent 24 hours
  (which live only in `summary.json`) and not the window counts inside a day.
- The very first publication has no published index to compare against, so it
  needs `--declare-new-epoch`.
- A store that predates store identity gets one the next time Weatherwatch
  initialises it (for example when the collector starts). It is marked `assigned_to_existing_store`, and its history is taken
  to begin at its first recorded run.
- PLC facts other than transitions report `coverage_fraction` 1.0 once they
  clear the disclosure floor, even when the export may cover only part of the
  window.
