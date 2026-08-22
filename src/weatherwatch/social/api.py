"""Wire format. A versioned, static JSON artifact -- there is no HTTP server.

weatherwatch publishes a rendered directory, not a service; `summary.json`
already sits beside `index.html` as its read side. `social.json` follows that
existing shape rather than inventing a server to stand behind it, because a
service is a different operational and privacy surface and this tranche is
measurement and observability only.

The projection is already audience-gated. This layer adds a schema version, a
generation stamp, and one more identity assertion at the last moment before
bytes are written -- the same reason `deploy/publish.sh` re-greps a directory
it has every reason to trust.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import timeutil
from .projection import (
    AUDIENCE_PUBLIC,
    SocialProjection,
    assert_identity_free,
)

SCHEMA = "weatherwatch.social/v1"
ARTIFACT_NAME = "social.json"


def build(projection: SocialProjection, generated_at: str | None = None) -> dict:
    doc = {
        "schema": SCHEMA,
        "generated_at": generated_at or timeutil.now_iso(),
        "disclaimer": (
            "Observed departures in aggregate event rates, as seen at one "
            "Jetstream endpoint. Episodes describe intervals, not accounts. "
            "Magnitude bands are provisional measurements, not conclusions, "
            "and co-occurrence is not causation."
        ),
        **projection.as_dict(),
    }
    if projection.audience == AUDIENCE_PUBLIC:
        assert_identity_free(doc)
    return doc


def write(
    projection: SocialProjection, out_dir: str | Path,
    generated_at: str | None = None, name: str = ARTIFACT_NAME,
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(
        json.dumps(build(projection, generated_at), indent=2, sort_keys=True),
        encoding="utf-8")
    return path
