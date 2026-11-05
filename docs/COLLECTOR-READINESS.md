# Collector startup observation

The guarded release selector must wait for the existing collector invocation
to execute the selected Python entry point. `systemctl start` returning does
not establish that its wrapper has completed exec. The offline reproduction
is `tests/collector_readiness_real.py`; run it over SSH with a real user
systemd bus and a new output directory. It creates only uniquely named
`ww-readiness-*.service` fixture units and stops those units explicitly after
capturing the observer result.

`scripts/collector_readiness.py CONFIG.json RECEIPT.json` is a read-only
apparatus helper. Configuration names `unit`, exact `argv`, exact `cwd`, exact
`module` origin, and `timeout` in seconds (greater than zero, at most 30).
`interval` defaults to 0.1 seconds; `user: true` selects the user systemd bus
for the offline fixture. Production selectors use the system bus.

The monotonic deadline bounds polling. The helper pins the first nonzero
MainPID, InvocationID and process start ticks, then refuses a change. Readiness
requires active/running state, the complete selected argv including protected
database and endpoint arguments, selected working directory, and the expected
resolved interpreter executable. It resolves the Weatherwatch module with that
interpreter in the observed process environment and directory, records only
the origin, and rechecks process and unit identity before success. Environment
bytes are never written to evidence. This establishes startup command and
module resolution, not collector ingestion or application health, and does not
inspect the already imported Python module object.

A selector saves the observation receipt on both success and refusal. On
refusal it leaves timers fenced and requires root reconciliation of the
original invocation and selectors. The helper never starts, stops, restarts,
restores selectors, or opens a product database. Existing release selection,
lease, resource, protected configuration and same-store rules still apply.

The offline fixture checks the original immediate release-path predicate
against the real delayed wrapper command. It then checks delayed success,
deadline expiry during exec delay, an already expired deadline, real unit
restarts changing PID and InvocationID, an isolated invocation-only observation
substitution, wrong working directory, wrong argv, wrong module origin and an
unavailable unit. The isolated comparison control is explicitly synthetic;
the surrounding process, SSH transport, and systemd observations are real.
Practical tests suffice for this bounded observer: it owns no producer or
recovery transition. Root owns any subsequent admission and acceptance.

The successor selector retained in the campaign evidence is a review template,
with the previous lease/run/revision constants deliberately retained. It is
not authorization to retry the failed run. A fresh candidate freeze and bound
admission must identify a new run, artifact, source, helper and selector hashes
before any production execution. The old failed verdict and artifacts remain
unchanged. Apparatus updates do not rebuild or requalify the product.
