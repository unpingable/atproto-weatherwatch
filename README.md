# weatherwatch — M0 verification spike

Working name, disposable. Aggregate ATProto/Bluesky event weather telemetry.
Design candidate lives in the workspace root:
`../CANDIDATE-AGGREGATE-WEATHER-TELEMETRY.md`.

**This repository currently contains M0 only** — a throwaway measurement and
fixture-generation spike. There is no service, no database, no classifier, no
dashboard. Do not build on `spike/`; it is meant to be deleted once M1 starts.

Results: [`M0-VERIFICATION-RESULTS.md`](M0-VERIFICATION-RESULTS.md).

## Privacy rule (non-negotiable)

```
live raw event -> inspect structure -> scrubbed fixture -> discard raw
```

Raw Jetstream events are inspected transiently in memory and never written to
disk. The committed fixture corpus preserves classifier-relevant *structure*
only: DIDs, rkeys, CIDs, URIs, handles, and all user-authored text are dropped
or replaced with obvious synthetic values. Synthetic DIDs use the W3C-reserved
`did:example:` method and synthetic hosts use RFC 2606 `.invalid`, so a
synthetic value can never be mistaken for a real one.

`spike/check_fixture_privacy.py` is the tripwire that proves it. It self-tests
against known-bad patterns before validating, so a validator that silently
always passes fails loudly instead.

## Layout

```
spike/m0_probe.py             survey / cursor / control probes
spike/scrub.py                deny-by-default structural scrubber
spike/check_fixture_privacy.py  privacy tripwire (+ --selftest)
fixtures/jetstream_shapes.jsonl    scrubbed live shapes
fixtures/jetstream_synthetic.jsonl hand-written malformed + scar fixtures
fixtures/malformed_lines.txt       non-JSON frames (parse-failure path)
measurements/                 aggregate-only measurements (no identity)
```

## Running

```bash
python3 spike/check_fixture_privacy.py          # always run this
python3 spike/m0_probe.py survey --seconds 600
python3 spike/m0_probe.py cursor --trials 5
python3 spike/m0_probe.py control
```

Requires `websockets`. No other dependencies.
