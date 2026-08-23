# Public Jetstream observers were not interchangeable in one paired probe

On 2026-08-08, Weatherwatch opened four concurrent, post-filtered Jetstream
connections from one host for the same 120-second wall-clock interval. The
purpose was to test an architectural assumption: whether two public observers
could be treated as interchangeable views for aggregate rate measurement.

| Observer | connections | posts observed |
|---|---:|---:|
| `jetstream2.us-east` | 2 | 4,400 on each |
| `jetstream1.us-east` | 1 | 7,088 |
| `jetstream1.us-west` | 1 | 6,928 |

The two independent sockets to `jetstream2.us-east` agreed exactly, including
their measured stream span. That same-endpoint self-control ratio was
**1.000**. Over the same interval, the observed post-volume ratio for
`jetstream1.us-east / jetstream2.us-east` was **1.611** (about 1.61×).

This establishes an inter-observer difference for that measured interval. It
does **not** establish coverage, completeness, set inclusion, or which observer
was closer to network truth. There was no authoritative denominator, and the
experiment retained aggregate counts rather than event identities, so it
could not compare the underlying event sets. The higher-volume stream may
have been a superset, the sets may have partially overlapped, or one observer
may have been temporarily degraded.

The operational consequence is narrow but important: aggregate rates must be
qualified as “observed at this named Jetstream endpoint.” Cross-observer volume
ratios are comparisons between observers, never estimates of network coverage.

## Reproduction record

- Full method and surrounding M0 results:
  [`M0-VERIFICATION-RESULTS.md`](../M0-VERIFICATION-RESULTS.md#cross-instance-completeness--falsified-as-an-assumption-of-completeness)
- Machine-readable aggregate result: [`measurements/instances2.json`](../measurements/instances2.json)
- Probe implementation: [`spike/m0_probe.py`](../spike/m0_probe.py)

No raw events or identity-bearing comparison set was retained for this probe.
