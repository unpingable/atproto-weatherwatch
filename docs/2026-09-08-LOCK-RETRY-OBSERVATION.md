# Lock retry: repair and extended production observation

2026-09-08. Runtime source: `1d62e2e441576e37267b06bce2288bac7dc4a6d7`
(`v0.1.0-rc.5`). This is a dated operational finding, following the repository's
practice of preserving verification results and correcting earlier conclusions
with later evidence.

## What failed and what changed

The optional social sink shares SQLite with scheduled field, publication, and
detector workloads. A competing writer can cause a sink flush to fail with
`database is locked`. The repair retains the batch after transaction failure,
retries after a bounded delay, and advances the successful flush sequence only
after commit. Buffer limits and the backpressure-drop counter remain relevant:
retry does not provide unlimited storage or eliminate contention.

The implementation and its failure regression are preserved in commit
`1d62e2e`; inspect `src/weatherwatch/social/store.py` and the social sink tests.
The earlier production acceptance window observed nine recovered collisions
and a gap-free success sequence 1..39. That supported bounded recovery under
contention, not indefinite operation.

## What the longer observation established

The September 8 follow-up inspected retained service logs and two bounded
ledger snapshots around 18:04–18:09 UTC. The later snapshot contains 180 rows
from the same collector run, with successful sequence 70..249, monotonic
seen/stored counters, and zero reported backpressure drops.

Between 15:24:55 and the follow-up, 22 failed flushes each have a later
successful ledger row. The maximum observed failure-to-success delay is
115.03 seconds. The maximum gap between successful ledger timestamps is
240.03 seconds; that different measure includes ordinary flush cadence.
Recurring contention remains visible, while the ledger supports retained-batch
catch-up in this interval.

Three natural detector runs at approximately 15:57, 16:57, and 17:57 UTC
reported 372, 372, and 370 episodes respectively. Field and publication jobs
continued successful scheduled completions. Those counts demonstrate actual
instrument operation; they do not independently validate the interpretation
of each detected episode.

## Limits and reproduction

The ledger does not persist peak pending-buffer depth or oldest pending-event
age. It cannot establish those historical values. Nor do cumulative counters
constitute an independent input ground truth, prove recovery of earlier lost
batches, or establish future freedom from loss.

To reproduce the local repair assertions, run
`PYTHONPATH=src python3 -m pytest tests/social/test_sink_integration.py`
at the recorded revision in its documented test environment. To repeat operational observation, compare successive
`sink_health` rows within one run: successful sequence, monotonic counters,
backpressure drops, and successful timestamps following failed-flush logs.
Observe scheduled field/publish/detector completion alongside those checks.

Exact selected production receipts and capture hashes are retained privately
under campaign `2026-09-08-observatory-correctness-recovery`, lane
`baseline-observation`. No production records or deployment configuration are
included in this repository account.

## Follow-up after the other observatories' cutovers

A further bounded capture at 20:03 UTC observed the unchanged RC5 runtime during
the cohort's new production workload. From the earlier 18:09 endpoint, the same
collector run advanced successful ledger sequence 249..304 with no gaps,
monotonic counters, and zero reported backpressure drops.

All 15 failed flushes in that interval had later successful ledger rows, at
most 115.004 seconds after failure. The selected error-class log entries shared
those failure timestamps. Contention remained observable and recoverable within
the captured interval.

Natural detectors at 18:57 and 19:57 UTC reported 367 and 362 episodes. Twenty-three
field jobs and twenty-three publication jobs completed; the latest pair finished
at 20:01 UTC. The aggregate cursor advanced beyond the 18:49 observation, and the
collector remained active with zero restarts. No manual job execution was used
to produce this evidence.

The same limits apply: ledger continuity and reported drop counts do not expose
peak buffer depth or oldest pending-event age, prove independent completeness,
or establish indefinite recovery. Exact selected receipts are retained in the
same private campaign lane under `WEATHER-POST-CUTOVER.md`. This account is a
documentation update; the observed runtime remains `1d62e2e`.
