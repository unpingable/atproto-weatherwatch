"""Read model. The only thing permitted to open the episode store for display.

Storage -> **projection** -> API -> visualisation. Nothing downstream of here
touches a collector or detector table, so the set of fields a reader can ever
see is decided in exactly one place: `EpisodeView`.

WHITELIST, NOT PASSTHROUGH
--------------------------
`EpisodeView` is built field by field from named keys. A sealed envelope's
`explain` block is a detector's own vocabulary and it grows; if the projection
forwarded it wholesale, the day a detector adds an identity-bearing key is the
day the published page grows one too. Constructing the view explicitly means a
new key is invisible until someone adds it here on purpose.

AUDIENCES
---------
* `public` -- the aggregate tier only. Those episodes are derived entirely
  from weatherwatch's identity-free minute counters, so there is no actor or
  target anywhere in their lineage. This is what the published report renders.
* `local` -- everything, including edge and lifecycle episodes. Not published;
  it is what an operator inspects on the collecting host.

The audience split is by **detector allowlist**, not by scrubbing. Scrubbing
asks "did I remember to remove the identity?"; an allowlist asks "is this
whole class of finding derived from data that never had any?" Only the second
question has a stable answer. `assert_identity_free()` runs anyway, as the
belt to the allowlist's braces, and it is the same pattern set the publish
script greps for before bytes leave the machine.

Note what the audience does *not* do: it does not decide whether identity is
removed. Because `EpisodeView` is a whitelist, salted actor tokens never reach
a view under either audience -- they exist only on the sealed envelopes in
storage, and in the local seismogram (`social/report.py`), which reads those
envelopes directly rather than going through here. So `local` means "more
detectors", not "fewer redactions".
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .scope import MAGNITUDE_BANDS

AUDIENCE_PUBLIC = "public"
AUDIENCE_LOCAL = "local"

#: Detectors whose findings may be published. Aggregate episodes are computed
#: from `bucket` counts, which cannot contain an actor or a target: the weather
#: lane's classifier discarded both before storage. Edge and lifecycle
#: detectors are structural and non-accusatory, but they are actor-level, so
#: they stay local.
PUBLIC_DETECTORS = frozenset({"aggregate_rate_episode"})

#: Explain keys known to carry actor-level structure. Not used for filtering --
#: the allowlist does that -- but asserted absent from public views.
IDENTITY_EXPLAIN_KEYS = frozenset({"actor_tokens", "target_token"})

#: Mirrors `deploy/publish.sh`'s privacy gate, plus salted actor tokens, which
#: the publish gate cannot know to look for.
IDENTITY_PATTERN = re.compile(
    r"did:(plc|web|key):"
    r"|at://"
    r"|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)"
    r"|\ba:[0-9a-f]{12}\b"
)

#: type -> the event family a reader groups by.
CATEGORIES = {
    "block": ("block_burst", "block_lull", "unblock_burst", "unblock_lull"),
    "like": ("like_storm", "like_lull", "unlike_burst", "unlike_lull"),
    "repost": ("repost_storm", "repost_lull", "unrepost_burst", "unrepost_lull"),
    "follow": ("follow_burst", "follow_lull", "unfollow_burst", "unfollow_lull"),
    "list": ("listadd_burst", "listadd_lull", "listremove_burst",
             "listremove_lull"),
    "delete": ("delete_storm", "delete_lull"),
    "account": ("account_inactive_burst", "account_inactive_lull"),
}
_TYPE_TO_CATEGORY = {t: c for c, ts in CATEGORIES.items() for t in ts}


class IdentityLeak(AssertionError):
    """Raised when a projection would carry an identifier. Never caught."""


@dataclass(frozen=True)
class EpisodeView:
    """Exactly what a reader may see. Adding a field here is a deliberate act."""

    # -- identity of the observation, not of anyone in it
    det_id: str
    episode_id: str
    evidence_id: str
    receipt_hash: str
    config_hash: str
    detector_id: str
    detector_version: str
    # -- when
    ts_start: str
    ts_end: str
    window: str
    n_windows: int | None
    # -- what
    type: str
    category: str
    direction: str
    metric: str
    # -- how big
    magnitude: float
    band: str
    rate_ratio: float | None
    # -- detector statistics, so the band can be ignored
    peak_z: float | None
    mean_z: float | None
    baseline_rate_eps: float | None
    extreme_rate_eps: float | None
    baseline_estimator: str
    events_in_episode: int | None
    # -- temporal shape
    rise_windows: int | None
    fall_windows: int | None
    peak_position: float | None
    # -- observation context, so a reader can discount a degraded stretch
    window_quality: tuple[str, ...]
    scope_n_windows: int | None
    scope_n_unobserved: int | None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["window_quality"] = list(self.window_quality)
        return d


@dataclass(frozen=True)
class SocialProjection:
    audience: str
    available: bool
    reason: str
    episodes: tuple[EpisodeView, ...] = ()
    summary: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    sink_receipt: dict | None = None

    def as_dict(self) -> dict:
        return {
            "audience": self.audience,
            "available": self.available,
            "reason": self.reason,
            "source": self.source,
            "sink_receipt": self.sink_receipt,
            "summary": self.summary,
            "episodes": [e.as_dict() for e in self.episodes],
        }


def assert_identity_free(payload) -> None:
    """Refuse to hand on anything matching an identifier shape."""
    blob = payload if isinstance(payload, str) else json.dumps(
        payload, sort_keys=True, default=str)
    hit = IDENTITY_PATTERN.search(blob)
    if hit:
        raise IdentityLeak(
            f"projection carries an identifier-shaped value: {hit.group(0)!r}")


def _band(magnitude: float) -> str:
    for floor, name in MAGNITUDE_BANDS:
        if magnitude >= floor:
            return name
    return "info"


def _view(row: sqlite3.Row, audience: str) -> EpisodeView:
    env = json.loads(row["envelope_json"])
    ex = env.get("explain", {})
    if audience == AUDIENCE_PUBLIC:
        leaked = IDENTITY_EXPLAIN_KEYS & set(ex)
        if leaked:
            raise IdentityLeak(
                f"{row['type']} carries {sorted(leaked)} and reached a public "
                f"projection; PUBLIC_DETECTORS is wrong")
    quality = ex.get("window_quality") or []
    return EpisodeView(
        det_id=env["det_id"],
        episode_id=env["subject"]["value"],
        evidence_id=ex.get("evidence_id", ""),
        receipt_hash=env["receipt_hash"],
        config_hash=env["config_hash"],
        detector_id=env["detector_id"],
        detector_version=env["detector_version"],
        ts_start=env["ts_start"],
        ts_end=env["ts_end"],
        window=env["window"],
        n_windows=ex.get("n_windows"),
        type=env["type"],
        category=_TYPE_TO_CATEGORY.get(env["type"], "other"),
        direction=ex.get("direction", ""),
        metric=ex.get("metric", ""),
        magnitude=env["score"],
        band=_band(env["score"]),
        rate_ratio=ex.get("rate_ratio"),
        peak_z=ex.get("peak_z"),
        mean_z=ex.get("mean_z"),
        baseline_rate_eps=ex.get("baseline_rate_eps"),
        extreme_rate_eps=ex.get("extreme_rate_eps"),
        baseline_estimator=ex.get("baseline_estimator", ""),
        events_in_episode=ex.get("events_in_episode"),
        rise_windows=ex.get("rise_windows"),
        fall_windows=ex.get("fall_windows"),
        peak_position=ex.get("peak_position"),
        window_quality=tuple(quality),
        scope_n_windows=ex.get("scope_n_windows"),
        scope_n_unobserved=ex.get("scope_n_unobserved"),
    )


def _latest_per_episode(rows: list) -> list:
    """One row per `episode_id`, keeping the most recently sealed.

    Re-running detection over a different range re-observes episodes that are
    already in the store. Those re-observations are genuinely distinct
    detections -- different scope, different coverage, different
    `window_fingerprint`, so a different `det_id` -- and storage keeps every
    one of them, because that is the audit trail.

    But `episode_id` is derived from the evidence segment alone, so it is
    stable across all of them: the same stretch of activity is the same
    episode however many times it is looked at. A reader wants one row per
    episode, not one row per time the detector ran.

    Measured on the deployed store: a second detection pass over a shifted
    window turned 1,885 rows into 2,242 while adding 9 actual episodes. Left
    alone, an hourly detector would inflate the page indefinitely.
    """
    latest: dict[str, object] = {}
    for r in rows:
        key = r["subject_value"]
        prev = latest.get(key)
        if prev is None or (r["sealed_at"], r["det_id"]) >= (
                prev["sealed_at"], prev["det_id"]):
            latest[key] = r
    return sorted(latest.values(), key=lambda r: (r["ts_start"], r["type"]))


def summarise(views: tuple[EpisodeView, ...]) -> dict:
    if not views:
        return {"n_episodes": 0, "by_type": {}, "by_band": {},
                "by_category": {}, "by_direction": {}, "magnitude": {},
                "first_ts": None, "last_ts": None, "detectors": []}
    by = lambda key: {  # noqa: E731
        k: sum(1 for v in views if getattr(v, key) == k)
        for k in sorted({getattr(v, key) for v in views})
    }
    mags = sorted(v.magnitude for v in views)
    mid = len(mags) // 2
    return {
        "n_episodes": len(views),
        "by_type": by("type"),
        "by_band": by("band"),
        "by_category": by("category"),
        "by_direction": by("direction"),
        "magnitude": {
            "min": mags[0],
            "median": mags[mid] if len(mags) % 2 else
                      round((mags[mid - 1] + mags[mid]) / 2, 6),
            "max": mags[-1],
        },
        "first_ts": min(v.ts_start for v in views),
        "last_ts": max(v.ts_end for v in views),
        "detectors": sorted({f"{v.detector_id}@{v.detector_version}"
                             for v in views}),
    }


def load(
    social_db: str | Path,
    audience: str = AUDIENCE_PUBLIC,
    since: str | None = None,
    until: str | None = None,
    limit: int = 5_000,
    sink_receipt: dict | None = None,
) -> SocialProjection:
    """Build the read model. Never raises on a missing or empty store."""
    path = Path(social_db)
    src = {"audience": audience, "since": since, "until": until,
           "detector_allowlist": (sorted(PUBLIC_DETECTORS)
                                  if audience == AUDIENCE_PUBLIC else None)}

    if not path.exists():
        return SocialProjection(
            audience=audience, available=False,
            reason="no episode store; run `weatherwatch social detect`",
            source=src, sink_receipt=sink_receipt)

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "episode" not in tables:
            return SocialProjection(
                audience=audience, available=False,
                reason="episode store has no detections yet",
                source=src, sink_receipt=sink_receipt)

        sql = "SELECT * FROM episode"
        clauses, params = [], []
        if audience == AUDIENCE_PUBLIC:
            marks = ",".join("?" * len(PUBLIC_DETECTORS))
            clauses.append(f"detector_id IN ({marks})")
            params.extend(sorted(PUBLIC_DETECTORS))
        if since:
            clauses.append("ts_end >= ?")
            params.append(since)
        if until:
            clauses.append("ts_start <= ?")
            params.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts_start, sealed_at LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    n_detections = len(rows)
    rows = _latest_per_episode(rows)
    views = tuple(_view(r, audience) for r in rows)
    summary = summarise(views)
    # Disclosed, not silent: the store may hold several sealed detections of
    # one episode and the page shows one row.
    summary["n_detections"] = n_detections
    summary["n_superseded"] = n_detections - len(views)
    proj = SocialProjection(
        audience=audience,
        available=bool(views),
        reason="" if views else "no episodes in range",
        episodes=views,
        summary=summary,
        source=src,
        sink_receipt=sink_receipt,
    )
    if audience == AUDIENCE_PUBLIC:
        assert_identity_free(proj.as_dict())
    return proj
