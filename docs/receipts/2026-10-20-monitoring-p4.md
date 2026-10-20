# Weatherwatch P4 monitoring source

Source base: deployed Weatherwatch `9daa4dc`. Result is the commit containing
this receipt. Implementation owner: P4 Weatherwatch lane; independent source
and testbed acceptance belongs to the parent integration lane. No production
configuration, database or host was changed by this lane.

The collector can opt into `--monitoring-snapshot PATH` and
`--monitoring-metrics PATH`. After its own successful committed runtime write it
reads its exact current run, the indexed latest window and endpoint cursor on
its existing connection. It does not open another SQLite connection, scan event
or bucket contents, run quick_check, or request a separate write transaction.
The snapshot and Prometheus file are separately replaced atomically, in that
order. Failed instrumentation is logged and cannot abort collection. The files
must be application-owned, in precreated parent directories; paths are explicit.
The deployed collector unit source opts into application-state paths. Installing
it still belongs to the existing deploy approval procedure.

The observation timestamp is the original writer observation, not scrape time.
Last success is the committed window end, never the heartbeat. Fresh heartbeat
without new committed output leaves last success unchanged. A committed empty
observed window is useful output; it supplies no invented event-lag zero. Thus
this declaration does not enable a cursor-lag limit. Snapshot observation age is
60 seconds; useful output age is 125 seconds for the deployed 60-second windows.
The conventional textfile carries `{service="weatherwatch"}`; the existing
host scrape supplies site. A separate generic observation-age rule must cover
stopped/frozen `.prom` output: scrape timestamp alone is insufficient.

The standalone observer is `python -m weatherwatch.monitoring --snapshot PATH
--report-dir DIR --format status|prometheus`. Optional `--prometheus-output PATH`
(`--metrics-output` alias) atomically writes the same standard metrics. It reads
at most 64 KiB of snapshot/summary JSON, fixed-size social SQLite headers and
file sizes, and a bounded current candidate content gate. It never opens SQLite.
The content gate permits at most 256 entries (including directories) and 16 MiB
of content; budget exhaustion or read failure yields UNKNOWN. The publisher's
existing mandatory unbounded gate remains unchanged by default. There is no new
gate receipt schema, server or metrics daemon.

`.ops/service.toml` is the authored P2 declaration, with the semantic contract
pin substituted by deployment. `.ops/monitoring-concerns.toml` states the new
scope; the original `.ops/concerns.toml` and legacy producer remain intact. The
packaged `_ops/monitoring-concerns.toml` is byte-identical to the new contract.

| Existing concern | New observation and limit |
| --- | --- |
| observation.coverage | Current committed window plus existing coverage-state semantics; missing/future/stale snapshot is unknown. |
| acquisition.connection | Bound runtime state and writer observation age; connection alone never proves output. |
| acquisition.cursor | Existing endpoint/cursor/window evaluator, including explicitly observed EMPTY. |
| acquisition.delay | Existing current event-lag evaluator; empty windows have no new lag sample. Ordinary lag metric remains available but no limit is declared for EMPTY. |
| observation.loss | Existing current-window known-loss and ambiguity evaluator. |
| persistence.access | Recent app-owned successful commit/read, **not whole-store quick_check integrity**. Legacy integrity remains uncovered by this faster path. |
| persistence.continuity | Existing structural/binding evaluator over fixed table-schema and exact run/cursor facts, not a whole-store audit. |
| persistence.social-store | Existing 100-byte header and absolute WAL-size bound, preserved separately from generic WAL-growth rules. |
| aggregate.production | Existing current-window semantics, plus ordinary last-success epoch and freshness limits. Ended runs do not establish present activity. |
| report.candidate | Current bounded summary source/age facts plus required-artifact existence; missing required content cannot clear. |
| publication.gate | Actual current bounded content gate, not summary-only or historical gate testimony. Changed identity-shaped content is refused even with unchanged summary. |

This is an alongside port, not an eleven-concern replacement claim. The old
856-line visibility module remains unchanged and supplies reused pure domain
classifiers; zero legacy lines are deleted. The new observer/writer adapter is
roughly 215 lines plus small collector/CLI integration and bounded gate support.
The meaningful subtraction is removal of database opening, whole-store integrity
scanning and rolled-back write-intent probing from scheduled new status reads.
No second copy of those slow database operations was moved into the collector.
Deleting legacy source waits for equivalent behavior and retained integrity scope.

The historical Weatherwatch producer timeout has no exact retained operation
trace in this source. This change does not claim to reproduce it or prove that
a particular SQLite operation caused it. Actual old/new timings and live
collector behavior belong to the parent P4 proof, not this source receipt. An
existing quiet-stream empty-window production defect is Weatherwatch #3; this
lane does not alter the accumulator or substitute a successful quiet-stream run.

Formalization consideration: bounded observer input cannot manufacture useful
progress from a heartbeat, missing/frozen/future snapshot, or historical gate
result. Tests separate observation and committed-window clocks, reject future
window/run binding errors, and check failed output replacement. Practical tests
and source review suffice for this bounded projection under a single app writer
and correctly functioning local clock/filesystem. These are not authenticated
external testimony, whole-database integrity, atomic two-file publication or
production-delivery guarantees. A bounded model of joint file publication would
be a separate future obligation if consumers require one; current consumers
explicitly use recorded freshness and unknown states. Decision owner: P4 source
lane, subject to independent parent acceptance.

Storage custody: root owns the campaign's aggregate 250 MiB envelope. Source,
small fixtures/tests and this receipt are the durable outputs. No VM, credentials,
compiler target, package installation or new dependency environment was created;
tests reuse the existing Weatherwatch virtualenv. Temporary test inputs are
owned by pytest/TemporaryDirectory; the parent closes aggregate test scratch
custody after source evidence is retained. No private application data copied.
Worktree retirement follows source/receipt custody and parent integration.

Validation at source delivery: 38 targeted monitoring/legacy-visibility tests
passed using the existing virtualenv and bytecode/cache writing disabled. These
include the consumer's observed_at/valid_for_seconds age handling for an
80-second-old valid window and 900-second-old valid candidate, source clock
facts retained separately, common runtime-clock/finite-width validation for
metrics/status, no SQLite observer open, interrupted output replacement, gate
content mutation/deletion and entry/byte budgets. Assessment timestamps never
refresh an invalid source into PRESENT. Python AST, TOML parsing and diff
whitespace checks passed. The full suite is also running in the foreground with
approved loopback socket access; its final outcome will be reported separately.
An earlier full run was interrupted with exit130 during ongoing source changes;
it was not treated as a passing run. No historical timeout was relabeled.
