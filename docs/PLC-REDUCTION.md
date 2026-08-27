# Campaign: PLC-REDUCTION-WITHOUT-IDENTITY-ESCAPE

Status: **PLC reduction V0 closed; no live PLC acquisition, source admission,
or publication claimed.**

The constitutional boundary is:

> **Identity enters the instrument and cannot leave.**

The supported reducer accepts the sequenced JSONL shape returned by the
official `plc.directory/export?after=0` interface. Those rows contain DIDs,
operation CIDs, keys, handles, service endpoints, signatures, and directory
timestamps. The [pinned DID PLC specification](https://github.com/did-method-plc/did-method-plc/blob/45a14801609e182afcd6907f95013cfb10381f73/website/spec/v0.1/did-plc.md#bulk-export)
documents that shape and also warns that historical handles and PDS locations
are permanently public, timestamps can aid correlation, service claims are not
cross-validated, and recovery can nullify prior operations. The reducer treats
the source accordingly; public source data is not privacy-neutral input.

## Pinned source contract

Weatherwatch admits one exact upstream document as its V0 parser contract:

- repository: `https://github.com/did-method-plc/did-method-plc`;
- revision: `45a14801609e182afcd6907f95013cfb10381f73`;
- path: `website/spec/v0.1/did-plc.md`;
- section: `Bulk Export`;
- document version: `v0.3.0`;
- specification SHA-256:
  `7346e2ba9d186fa13d65466942f29136f2d3e2281a8a6dd08d689eda2c99af79`.

The immutable source is
[`did-plc.md` at the admitted revision](https://github.com/did-method-plc/did-method-plc/blob/45a14801609e182afcd6907f95013cfb10381f73/website/spec/v0.1/did-plc.md#bulk-export).
The machine-readable pin and consumed assumptions are in
`.ops/plc-source-contract.json`, under
`weatherwatch.plc.source-contract/v1`.

V0 requires the integer-sequence export obtained beginning with `after=0`,
exact top-level fields `type`, `operation`, `did`, `cid`, `createdAt`, `seq`,
strictly increasing sequence numbers, and one of the three admitted operation
types. It consumes only operation type, previous-operation presence, and the
ATProto PDS service endpoint. It does not authenticate the artifact, verify a
CID/signature, or validate the full PLC operation. The legacy timestamp export
refuses. Any future upstream revision or shape change requires an explicit
contract update and requalification; mutable `main` is not an admission rule.

Tests construct minimal synthetic rows matching this shape. No live or copied
PLC identity is committed as a fixture.

## Supported interface

```bash
weatherwatch plc-reduce \
  --input plc-sequenced-export.jsonl \
  --acquired-at 2026-08-27T18:00:00Z

weatherwatch plc-reduce \
  --input plc-sequenced-export.jsonl \
  --acquired-at 2026-08-27T18:00:00Z \
  --bundle-only > plc-facts.json
```

The full output is `weatherwatch.plc.reduction/v1`, described by
`.ops/plc-reduction.schema.json`. `--bundle-only` emits the nested
`weatherwatch.composition.bundle/v1` document for the generic compositor. The
source contract remains separately repository-owned in
`.ops/composition-contracts.json`; the reducer cannot admit or amend it.

The acquisition time is required. PLC `createdAt` assigns the event to an
event-time window; `acquired_at` records when this exact source artifact became
known to Weatherwatch. A later backfill may revise an old event-time count, but
the resulting fact carries the later acquisition time. Historical knowledge is
never backdated.

## What crosses the boundary

The reducer emits only these aggregate measurements:

| measurement | bounded meaning |
|---|---|
| `plc.directory.operations` | sequenced operations present in the supplied export artifact |
| `plc.directory.creations` | supplied operations whose installed shape is a genesis creation |
| `plc.directory.tombstones` | supplied tombstone operations |
| `plc.directory.endpoint_mutations` | comparable consecutive operations for one transiently held DID had different endpoint state |
| `plc.directory.migration_like_transitions` | a comparable endpoint mutation changed between two syntactically qualified HTTPS PDS origins |

Every fact has an event-time window and acquisition time. All facts remain
`DEGRADED` even when disclosed because they are conditioned on the supplied
export artifact, service endpoints are not independently verified, sequenced
export does not state nullification status, and directory completeness is not
established. A valid file is not magically “all PLC.”

An endpoint mutation is not a migration. A migration-like transition is not a
successful migration, provider change, or evidence of follow-through. The
reducer emits no population denominator.

## Disclosure and temporal-fingerprinting policy

- publication windows are fixed, non-overlapping UTC weeks;
- narrower windows refuse rather than silently widen;
- the minimum disclosure count is 10 and callers may only raise it;
- within each individual measurement fact, values from zero through nine all
  become the same `UNKNOWN`, `value: null` representation;
- at most 104 complete windows are emitted in one artifact;
- an incomplete current week is omitted;
- provider, origin, `from`/`to`, cohort, or pseudonymous dimensions do not
  exist in the output contract;
- an event contributes to one non-overlapping window, never a sliding sequence.

### Composition result: global non-disclosure is not qualified

The five measurements have real subset relationships:

```text
migration-like transitions <= endpoint mutations <= operations
creations <= operations
tombstones <= operations
creations + tombstones + endpoint mutations <= operations
```

Independently disclosed facts can therefore expose a small complement. For
example, `endpoint mutations = 12` and `migration-like transitions = 10`
reveals two endpoint mutations outside the migration-like subset. Likewise,
`operations = 11` and `creations = 10` reveals one non-creation operation.
The adversarial suite also constructs a derived `1,0,1` weekly sequence while
every participating fact is itself at or above threshold.

Repeated acquisitions create another arithmetic channel. Two disclosed
versions of the same event window, `10` then `11`, reveal a one-operation
revision. `UNKNOWN -> 10` and `10 -> UNKNOWN` reveal threshold crossing and a
bounded range, not an exact prior/next count; `10 -> 11` and `11 -> 10` reveal
exact deltas.

No small count is emitted directly in these cases, but an observer can derive
a small aggregate count or revision delta. That is not by itself recovery of a
DID, linkage of an individual across windows, or proof that one particular
public PLC operation caused the difference. It can become useful event/backfill
fingerprinting when combined with auxiliary public history.

The exact qualified disclosure claim is:

> **Per-fact low-count suppression is qualified. Compositional non-disclosure
> across multiple facts or publication revisions is not claimed.**

The output records this limit under `disclosure_claim`. Preventing the channel
would require coordinated release accounting or a material publication
redesign; V0 does neither. Provider-flow publication would likewise require a
separate campaign; V0 refuses provider dimensions entirely.

## Transient resource and process custody

The batch parser refuses before attacker-controlled work or state exceeds:

| resource | bound | purpose |
|---|---:|---|
| decoded JSONL row | 32,768 bytes | bounds parser input while allowing expansion over the upstream 7,500-byte DAG-CBOR operation limit |
| retained endpoint string | 2,048 bytes | bounds the variable-length identity-bearing value retained across rows |
| JSON nesting | 64 | bounds parser-stack/pathological nesting |
| operations per invocation | 1,000,000 | bounds per-batch CPU work |
| distinct transient identity histories | 250,000 | bounds the DID-indexed memory map |
| tracked event windows | 4,096 | bounds the aggregate window map |

These are defensive implementation ceilings, not population estimates,
publication thresholds, or a claim that a full PLC export fits in V0. The
reducer refuses instead of spilling identity history to disk. Designing a live
or full-history acquisition process would require a separate capacity and
custody decision.

Oversized rows, endpoints, operation streams, transient identity sets, event
window sets, and pathological nesting produce fixed non-echoing refusals.
`SIGINT` and `SIGTERM` are temporarily converted to `INTERRUPTED_SIGINT`
(exit 130) and `INTERRUPTED_SIGTERM` (exit 143), allowing the reducer's
`finally` cleanup to run. Original handlers are restored afterward.

## Custody mechanics

The reducer's only identity-indexed state is an in-memory mapping from DID to
the immediately preceding endpoint state. It exists solely during one batch
call and is cleared in a `finally` block on success or refusal. No DID, handle,
CID, key, signature, endpoint, provider tuple, stable hash, or pseudonym has a
field in the output schema.

The supported CLI temporarily sets the process core-dump limit to zero before
opening raw input and keeps it disabled through parser exceptions, traceback
disposal, refusal rendering, and safe output serialization. It restores the
limit afterward. If core-dump or signal custody cannot be established,
reduction refuses. Parser, custody, and unexpected application failures produce
machine-readable refusals containing only a stable code, structural path, and
exception class where relevant; input values are never echoed or logged.

The reducer does not delete or take custody of the caller's source file. The
acquisition process must place raw export material on suitably protected,
ephemeral storage and remove it under its own explicit retention policy. No
production acquisition process is added by this campaign.

The boundary does not provide secure memory erasure and makes no claim about
swap/page-out, `ptrace` or debugger access, privileged host inspection, kernel
crash capture, hostile hypervisor/VM introspection, source-export copies outside
the reducer, or forensic recovery of allocator/process memory. “Identity enters
the instrument and cannot leave” names the supported reducer/output boundary,
not host, kernel, or virtualization confidentiality.

## Positive invariant

> **Given persisted reducer output alone, no supported operation can recover or
> link an individual PLC identity across windows.**

The proof is structural within the supported model:

1. the persisted schema contains aggregate counts, bounded times, coverage,
   and fixed semantic/custody receipts only;
2. every fact has empty dimensions;
3. within each fact, small and zero identity-derived cells have an identical
   representation;
4. windows do not overlap;
5. no stable per-source row, cohort token, provider tuple, or hash is emitted;
6. the compositor has no actor join or generic dimension join.

This does not claim that the public PLC source itself is anonymous. It is
explicitly identity-rich. It also does not claim that arithmetic over aggregates
cannot reveal a small count; that negative result is independent of structural
identity linkage.

## Refused claims

- “N users migrated” or “N successful migrations”;
- migrant follow-through or post-migration activity;
- `writes / PLC identities` as production per capita;
- PLC identities as network population or DAU;
- provider concentration or provider flow from these facts;
- endpoint changes as proof of changed hosting provider;
- counts as a fraction of all migrations;
- an old revised window as knowledge available at its event time;
- source completeness, cryptographic verification, or admission.

No next implementation is begun by this closeout.

## Deferred questions, not active work

- live PLC source acquisition and admission;
- any unresolved compositional-disclosure issue requiring coordinated releases;
- a real composite publication from admitted sources;
- whatever genuinely interesting empirical question appears only after people
  use the instrument.

These are notes for future judgment, not commitments, schedules, or authority
to expand V0.

## Closeout evidence — 2026-08-27

- entry repository: `/home/jbeck/git/atproto-nutrition/weatherwatch`;
- branch/upstream: `main` tracking `origin/main`, initially 0 ahead / 0 behind;
- entry HEAD: `e634facfb56dda2e9a94ed3b7fede76777844a32`;
- entry dirty state: the expected unstaged/untracked combined Weatherwatch
  visibility, finding-led publication, temporal-composition, and PLC campaign;
  no staged or unrelated path;
- unchanged entry PLC/composition baseline: 44 passed;
- final focused PLC/composition suite: 70 passed;
- restricted full suite: 696 passed, with only five environment-denied
  loopback binds;
- exact loopback collector module in the permitted context: 5 passed;
- effective behavioral coverage across required contexts: 701/701;
- source/spec static checks: 87 Python files pass the Python 3.10 syntax guard;
- fixture privacy tripwire: 344 lines clean;
- schema qualification: all 9 schemas structurally valid and 5 representative
  PLC/composition documents validate;
- compilation and `git diff --check`: pass.

No qualification step downloaded live PLC rows. The only upstream download was
the public specification text at the pinned revision, used to verify its
section and SHA-256 outside the repository.
