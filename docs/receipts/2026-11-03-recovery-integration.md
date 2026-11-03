# Restart duration and observation-store recovery integration

This continues [issue 3](https://github.com/unpingable/atproto-weatherwatch/issues/3),
[issue 5](https://github.com/unpingable/atproto-weatherwatch/issues/5), and
Weatherwatch's part of [Driftwatch issue 2](https://github.com/unpingable/atproto-driftwatch/issues/2).
The owner authorized this backlog work on November 3. Jev and Phlogiston are
excluded. The prior window-finalization receipt preserves its original scope
and results; this receipt records the later integration.

The restart denominator now deducts the union of known gap intervals clipped
to the actual observed span. It retains the full diagnostic gap, resume seam,
baseline exclusion and atomic window/cursor behavior. Unknown-position duration
signals remain conservative. A gap before the first event no longer reduces
a later observed 60- or 50-second span to zero. Repeated discard/replay can
still conservatively understate duration; no stronger recovery claim is made.

The existing recovery source adds a durable observation-store generation,
unknown continuity on a cold start, endpoint/store-bound health checkpoints,
and a publication guard against silently shortened history, changed stores,
unknown published baselines or recent observed-duration loss. An explicit epoch
declaration carries the discontinuity. Deployment identity, operational
credentials and configuration retain their existing custody. An observation
database is not account identity or a substitute for that custody.

Integrated source `6fb98c156a2d0fce5826f61f994f7f54e34b9302`, tree
`d5994edfa49833097d12788dbc92fd316b10925c`, passed the complete qualification:
**782 passed, 14 skipped**, plus compile, Python 3.10 syntax and fixture checks.
The 101 focused cases cover gaps, real collector replay, lineage, publication
continuity and visibility. Restoring the old scalar deduction produces nine
expected failures with four controls passing. No old rows or verdicts changed.

An earlier complete run had one failure in the existing bounded-memory test
and remains failed. A deterministic offline reproduction isolated unequal
CPython allocator-cache state. Collecting before each traced sample removes
that measurement dependency; substituting an unbounded sealer batch still
fails the repaired test. The sealer and threshold are unchanged. The first
independent review was INCONCLUSIVE on that failed result. The final independent
Claude review accepted the frozen integrated candidate with no important
findings after the repaired full run. This commit adds only this closeout prose
and consistent publication wording following the final review.

Evidence is retained under the shared crow lane
`/data/git/.lanes/atproto-backlog-20261103/weatherwatch/`: `duration-1` preserves
the failed run and controls; `integrated-final-1` owns the successful run,
manifest, final review and exact-history bundle. The reviewed manifest SHA-256
is `580fd823308b9c3697a4377edb899177a2aecabbd48a2b04e54f110e7fbf945c`.
Source delivery may use a different commit identity through GitHub's available
write path; the delivery receipt must verify the entire final tree, preserve
the exact original graph and distinguish source equivalence from image or
deployment identity. Production selection and observed behavior are recorded
separately in Cartography and the issues.

For this bounded transition, concrete replica tests and the real collector and
publication consumers establish the claimed behavior. A separate model would
not establish provider replay completeness or production continuity. The owner
retains generation-rollover and production acceptance decisions.
