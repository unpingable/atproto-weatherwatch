"""Social event sensors — episode detection over the existing observation layer.

This package is an *analysis layer*, not a second observatory. It adds no
Jetstream consumer, no second event schema, and no second detection object:

    Jetstream
        |
        v
    weatherwatch collector          <- custody + normalisation (unchanged)
        |
        +--> classify() -> buckets  <- weather views (unchanged, identity-free)
        |
        +--> social sink -> edges   <- THIS PACKAGE, opt-in, off by default

The subject of analysis is the **episode** — a bounded stretch of observed
activity — never an account. See `BOUNDARIES.md` for what that forbids and
why the split exists at all.

Two sensor tiers, one envelope:

* **aggregate** — reads the buckets weatherwatch already persists. No new
  retention whatsoever. Answers "did the network-level rate of some event
  class depart from its own trailing baseline, and for how long."
* **edge** — reads the opt-in edge store. Answers the questions that are
  arithmetically impossible from counters: concentration, overlap,
  synchronisation. Requires actor->target pairs, which `classify()` cannot
  emit by construction (its output alphabet is a finite metric set).

Both emit `DetectionEnvelope`.
"""

from .envelope import (  # noqa: F401
    DetectionEnvelope,
    EvidenceStub,
    SubjectRef,
    build_envelope,
    envelope_to_dict,
    receipt_hash,
    stable_json,
    validate_envelope,
)

SOCIAL_SCHEMA_VERSION = 1
