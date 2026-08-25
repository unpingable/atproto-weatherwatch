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

SCHEMA VERSIONS
---------------
`v3` adds `instrument`, `subject_types`, `measures` and `does_not_measure`
alongside the existing prose `disclaimer`. The change is purely additive, so a
`v2` reader keeps working; the version moved anyway, because a schema string
that stays put while the schema changes is worse than no version at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import COLLECTOR_VERSION, timeutil
from .projection import (
    AUDIENCE_PUBLIC,
    SocialProjection,
    assert_identity_free,
)

SCHEMA = "weatherwatch.social/v3"
ARTIFACT_NAME = "social.json"

#: The bounded set of things a row in this artifact can be about. One member,
#: and that is the point: `episode` is the only subject the detection layer
#: admits, and a reader who takes a row for a statement about an account has
#: misread the artifact rather than found a hidden field.
SUBJECT_TYPES = ("episode",)

#: The same pair `summary.json` carries, because `social.json` is fetched on
#: its own and a consumer that never opens the page has no other route to it.
#: The misread this exists to prevent is specific and was observed on the
#: weather lane: a reader given the page concluded conflict monitoring and had
#: to reason its way back out. Prose in `disclaimer` cannot be parsed; these
#: can.
MEASURES = (
    "departures in the rate of aggregate ATProto events, as observed at one "
    "named Jetstream endpoint, reduced to coarse periods and magnitude bands"
)
DOES_NOT_MEASURE = (
    "conflict or disputes",
    "sentiment or affect",
    "individual users or accounts",
    "coordination, intent, or motive",
    "post content or text",
    "the social graph",
    "any identity",
    "geographic origin",
)


def build(projection: SocialProjection, generated_at: str | None = None) -> dict:
    doc = {
        "schema": SCHEMA,
        "generated_at": generated_at or timeutil.now_iso(),
        "disclaimer": (
            "Disclosure-limited departures in aggregate event rates, as seen "
            "at one Jetstream endpoint. Public periods passed a provisional "
            "local actor-support gate and were time-coarsened; this is "
            "disclosure resistance, not anonymity. Magnitude bands are "
            "provisional measurements, not conclusions, and co-occurrence "
            "is not causation."
        ),
        "instrument": {
            "id": "weatherwatch",
            "collector_version": COLLECTOR_VERSION,
        },
        "subject_types": list(SUBJECT_TYPES),
        "measures": MEASURES,
        "does_not_measure": list(DOES_NOT_MEASURE),
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
