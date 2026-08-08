"""weatherwatch — aggregate ATProto event weather telemetry.

Counts network-level activity rates from a Jetstream source and persists only
aggregate counters plus observation-health metadata. No raw events, no DIDs,
no handles, no rkeys, no CIDs, no URIs, no user text — ever, anywhere.
"""

COLLECTOR_VERSION = "0.1.0"

__all__ = ["COLLECTOR_VERSION"]
