# Issue 3: stalled-stream window finalization

Issue: <https://github.com/unpingable/atproto-weatherwatch/issues/3>.
Work continues on `fix/stall-replay-20261028`, starting at `2af1973`,
with product repair `94fc4de` on base `9daa4dc`. The owner is unpingable;
Codex owns this bounded implementation and verification occurrence.

## Decision and scope

The accumulator receives stream timestamps and a wall clock. Neither a
receive timeout nor a successful WebSocket keepalive proves that the relay
has finished delivering a minute. The existing repair therefore removes
the wall-clock finalization transition. An open window remains replayable
until stream time advances; shutdown commits only the span through its
last observed event. A quiet connection also waits for stream progress.
It cannot emit trustworthy complete empty windows without a source
completeness watermark. This deliberately narrows the old silence claim.

A recent-frame heuristic still leaves a stall-detection interval. Persisted
provisional windows would require new storage and report eligibility
semantics. Retroactively marking old windows partial would not recover
discarded events. Waiting for stream progression uses the existing atomic
window/cursor transaction and preserves replay inputs without either
addition.

The repair also checks resume gaps on the first connection of a restarted
collector. Historical rows and cursors are preserved, including old explicit
empty rows. A restart discloses a resume seam and any missing replay interval
without adding an in-run reconnect to the published counter. Recovery and the observation limitation are documented in
[`../VISIBILITY.md`](../VISIBILITY.md#recovery-from-premature-wall-clock-finalization)
and [`../../README.md`](../../README.md#observation-limits).

## Completion plan

1. Restore the issue's regression matrix with the conservative silence
   contract, genuine catch-up, already committed replay, and stall recovery
   both with and without reconnect.
2. Exercise actual `Collector.run`, loopback WebSockets, receive timeouts,
   SQLite commits, cursor selection, and the visibility consumer together.
   Verify that a fresh connected heartbeat cannot refresh useful output.
3. Restore only the old `tick()` implementation in a separate test process
   as a deterministic negative control. Run the complete repository check,
   review the source and evidence independently, and record the outcome.
4. Publish the established branch with the verified tests and this receipt.
   Main integration and production deployment remain separate decisions.

## Evidence

The shared lane registry is
`/data/git/.lanes/weatherwatch-issue3-20261103/REGISTRY.json`.
Runs `verification-1` and `verification-2` own the bounded logs, negative-control driver,
review packets and terminal receipts beneath that directory. The registry
records their purpose, custody and retirement condition. These are crow
evidence paths; they are not client access links.

The seven new regression cases pass against the repaired source. Restoring
only `Accumulator.tick` from `9daa4dc` in memory produces five assertion
failures: silence finalization, both stall cases, and both real collector
cases. Genuine catch-up and committed replay still pass. The negative
control edits no product files and changes no historical result.

The initial system-Python attempt could not collect six modules because
`websockets` was absent. The existing project virtual environment supplies
Python 3.12.3, pytest 9.1.1 and websockets 17.1. A subsequent sandboxed run
finished with 727 passed, 14 skipped and five failed existing collector
tests because the sandbox cannot bind loopback sockets. Those failures are
environment limitations, not passing results. The authoritative rerun uses
that virtual environment with loopback access.

The authoritative command completed successfully:

```sh
WW_PYTHON=/data/git/atproto-nutrition/weatherwatch/.venv/bin/python ./scripts/qualify.sh
```

Initial-candidate result: **739 passed, 14 skipped** in 306.95 seconds. Compilation, the
Python 3.10 f-string compatibility check and all 344 fixture lines passed.
`scripts/verify-neutral-instruments.py` separately verified the seven pinned
asset files. Receipt links and anchors and `git diff --cached --check` passed.
This is Python 3.12.3 verification; it is not a run of the complete CI
interpreter matrix.

Claude Opus 5.5 independently reviewed the whole repair through the
standing-authorized `api-review.sh` helper. The first verdict was
`ACCEPT_WITH_CHANGES`: the stall repair held, but the first cursor resume of
a new process incorrectly incremented the in-run reconnect counter. Two
new assertions failed before the counter correction; all nine focused
restart/stall cases passed afterward. Real-loop assertions also preserve
the actual in-run reconnect count. The README now explicitly identifies
whole skipped minutes as unobserved, without invented rows.

The original review and full-check result remain in `verification-1` with
an immutable `COMPLETE.json`. `verification-2` pins the corrected source
and tests in `INPUTS.sha256`; its full-check log begins with the command,
interpreter, staged tree and manifest identity. Its source diff excludes
this evolving closeout prose.

The final candidate completed the same authoritative command with
**740 passed, 14 skipped in 300.69 seconds**, with all preceding stages
passing. The pinned asset and receipt checks also passed and are retained
in `verification-2/ancillary-checks.log`.

The second independent verdict was **ACCEPT**, conditional on that final
full-suite run succeeding. Its terminal result satisfies the condition;
the review reported no blocking or important findings. Source and test
hashes still match the reviewed manifest
`bb9e621b56cb6dce94571ecfc799d6c3522c366a938c5cf8b657f3b07dc1819a`.
Raw verdicts, review identities and test results are retained beside each
run's terminal record. This is reviewed internal development, not production
acceptance or authority to merge, restart services or publish a release.

## Bounded claim and remaining limits

The proposition is that elapsed wall time alone cannot advance the
accumulator's committed-window high water or durable event cursor. In the
specified stall/replay traces, all six events in the affected minute are
committed once, without late-event loss. Connection testimony and aggregate
freshness are evaluated separately. This relies on the existing measured
Jetstream ordering and inclusive cursor semantics; it does not establish
global relay completeness, recovery beyond replay retention, or correctness
of historical coverage claims.

Replay recovery requires an existing durable cursor. Before the first
committed window, a reconnect may return to the live tail; the interrupted
interval is partial or unobserved. A socket that keeps answering pings
without sending events can remain connected indefinitely; the aggregates
become stale, and this change adds no silence watchdog.

The review also identified a separate conservative duration undercount:
on restart, a cursor-to-first-event gap can be deducted after the observed
span has already started at that first event. The gap remains a coverage
defect, so this does not make the window eligible as complete observation.
The arithmetic correction is proposed follow-up
[#5](https://github.com/unpingable/atproto-weatherwatch/issues/5), with
reproduction, dependencies and completion criteria; it is not admitted
implementation work in this occurrence.

The removed transition, deterministic clock controls, real loopback tests
and SQLite assertions give a small directly executable state-transition
model. A separate formal model would duplicate this bounded claim without
verifying the WebSocket or persistence consumers, so practical tests and
this statement suffice. Unpingable retains integration and deployment
authority. No production restart, historical reclassification, data-store
rollover or public publication is part of this occurrence.
