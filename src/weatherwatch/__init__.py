"""weatherwatch — aggregate ATProto event weather telemetry.

The weather lane counts network-level activity rates from a Jetstream source
and persists only aggregate counters plus observation-health metadata. Its
database carries no raw events, DIDs, handles, rkeys, CIDs, URIs, or user text.
The optional bounded social edge sink is separate and documented explicitly.
"""

COLLECTOR_VERSION = "0.1.0"

__all__ = ["COLLECTOR_VERSION"]
