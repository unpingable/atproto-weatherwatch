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
* `public` -- aggregate episodes only, and only after a local cardinality gate.
  Exact statistics and timing are removed before this view is returned.
* `local` -- everything, including edge and lifecycle episodes. Not published;
  it is what an operator inspects on the collecting host.

The audience split begins with a **detector allowlist**, then applies an actor
support gate and a lossy public projection. No DID is emitted, but that fact
alone is not treated as anonymity: a precise aggregate can still be joined to
the public firehose. `assert_identity_free()` remains the final field-shape
tripwire and the publish script greps the bytes again before they leave.

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
from datetime import datetime, timedelta, timezone
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

#: Public disclosure is gated by actor support observed in the already-local
#: edge store. This is deliberately an account-cardinality rule, not an event
#: count rule: one account can create many events. Ten is a provisional
#: disclosure-resistance floor, not a statistical claim and not anonymity.
#:
#: **What this counts, exactly.** Distinct actors performing the same
#: collection and operation *anywhere in the observed stream* during the
#: episode interval. That is ambient cardinality, not the cardinality of the
#: departure. On a live network the two diverge badly: `block.create` runs
#: around 5/s, so any interval contains hundreds of unrelated actors and the
#: floor is satisfied no matter who produced the excess. Demonstrated in
#: `test_a_one_actor_excess_is_not_excluded_by_ambient_cardinality_alone`.
PUBLIC_MIN_ACTORS = 10
PUBLIC_TIME_BUCKET_S = 3600

#: Aggregate metrics for which the edge store can independently witness actor
#: support. Anything absent here is suppressed. This mapping is bounded and
#: contains no collection names supplied by an event.
PUBLIC_SUPPORT_METRICS = {
    f"{collection}.{op}": (collection, op)
    for collection in ("block", "follow", "like", "repost", "listitem")
    for op in ("create", "update", "delete")
}

#: Second gate, for `excess` episodes only, closing what the cardinality floor
#: above does not: an episode whose entire departure could have been produced
#: by one account.
#:
#: The rule needs no invented constant. The detector already records how many
#: events fell inside the episode and what the baseline rate was, so the
#: *excess* over baseline is arithmetic. If any single actor emitted at least
#: that many events of the same collection and operation during the interval,
#: that actor alone could account for the whole departure, and publishing the
#: episode points an observer at an interval they would otherwise have to find
#: for themselves. Such episodes are suppressed.
#:
#: Deliberately not applied to `deficit` episodes: a lull is an absence, and no
#: single account can account for events that did not happen. Deficits keep the
#: cardinality floor alone.
#:
#: This is a threshold-free comparison, not a concentration score, and it
#: computes no per-actor output: the query returns one integer, the largest
#: per-actor event count, and no actor value leaves this module.
DOMINANCE_GATE = "single actor could account for the whole excess"

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
class PublicEpisodeView:
    """The deliberately lossy public representation of an episode.

    Exact timestamps, counts, rates, z-scores, shape, and stable identifiers
    are omitted because their combination can make a nominally aggregate row
    trivially joinable to publicly observable ATProto activity.
    """

    period_start: str
    period_end: str
    type: str
    category: str
    direction: str
    band: str
    actor_support: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SocialProjection:
    audience: str
    available: bool
    reason: str
    episodes: tuple[EpisodeView | PublicEpisodeView, ...] = ()
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


def _coarse_period(ts_start: str, ts_end: str) -> tuple[str, str]:
    """Round an episode outward to UTC hour boundaries."""
    start = datetime.fromisoformat(ts_start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(ts_end.replace("Z", "+00:00"))
    start = start.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    end = end.astimezone(timezone.utc)
    end_floor = end.replace(minute=0, second=0, microsecond=0)
    if end > end_floor:
        end_floor += timedelta(seconds=PUBLIC_TIME_BUCKET_S)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end_floor.isoformat().replace("+00:00", "Z"),
    )


def _public_view(conn: sqlite3.Connection, row: sqlite3.Row) -> PublicEpisodeView | None:
    """Return a disclosure-resistant view, or suppress the row.

    The edge query is only an eligibility gate. Actor identifiers never leave
    this function. Missing tables, unsupported metrics, malformed envelopes,
    expired retention, and insufficient support all resolve to suppression.
    """
    try:
        env = json.loads(row["envelope_json"])
        ex = env.get("explain", {})
        support_key = PUBLIC_SUPPORT_METRICS.get(ex.get("metric"))
        if support_key is None:
            return None
        start_epoch = datetime.fromisoformat(
            env["ts_start"].replace("Z", "+00:00")).timestamp()
        end_epoch = datetime.fromisoformat(
            env["ts_end"].replace("Z", "+00:00")).timestamp()
        collection, op = support_key
        support = conn.execute(
            "SELECT COUNT(DISTINCT actor_did) FROM edge_event "
            "WHERE collection=? AND op=? AND observed_us>=? AND observed_us<?",
            (collection, op, int(start_epoch * 1_000_000),
             int(end_epoch * 1_000_000)),
        ).fetchone()[0]
        if support < PUBLIC_MIN_ACTORS:
            return None
        if ex.get("direction") == "excess" and not _excess_is_distributed(
                conn, collection, op, start_epoch, end_epoch, ex):
            return None
        start, end = _coarse_period(env["ts_start"], env["ts_end"])
        return PublicEpisodeView(
            period_start=start,
            period_end=end,
            type=env["type"],
            category=_TYPE_TO_CATEGORY.get(env["type"], "other"),
            direction=ex.get("direction", ""),
            band=_band(env["score"]),
            actor_support=f"{PUBLIC_MIN_ACTORS}+",
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        return None


def _excess_is_distributed(conn, collection, op, start_epoch, end_epoch,
                           ex: dict) -> bool:
    """False when one actor could have produced the whole excess.

    The cardinality floor above counts *ambient* actors in the interval, which
    on a live network is satisfied by unrelated traffic regardless of who
    caused the departure. This closes that: if the busiest single actor in the
    interval emitted at least as many events as the episode's excess over
    baseline, the episode is one account's activity wearing an aggregate's
    clothes, and publishing it narrows an observer's search to that hour.

    Fails closed. A missing count, an unusable baseline, a zero-length
    interval, or a database error all return False, because "cannot tell" and
    "safe to publish" are different answers.

    No actor value is read out. The query returns one integer.
    """
    events = ex.get("events_in_episode")
    baseline_eps = ex.get("baseline_rate_eps")
    if not isinstance(events, int) or not isinstance(
            baseline_eps, (int, float)):
        return False
    duration_s = end_epoch - start_epoch
    if duration_s <= 0:
        return False
    excess = events - (baseline_eps * duration_s)
    if excess <= 0:
        # Labelled an excess but not measurably above baseline. Nothing to
        # attribute, and nothing to publish either.
        return False
    top = conn.execute(
        "SELECT COUNT(*) AS n FROM edge_event "
        "WHERE collection=? AND op=? AND observed_us>=? AND observed_us<? "
        "GROUP BY actor_did ORDER BY n DESC LIMIT 1",
        (collection, op, int(start_epoch * 1_000_000),
         int(end_epoch * 1_000_000)),
    ).fetchone()
    if top is None:
        return False
    return top[0] < excess


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


def summarise_public(views: tuple[PublicEpisodeView, ...]) -> dict:
    if not views:
        return {"n_disclosed": 0, "by_type": {}, "by_band": {},
                "by_category": {}, "by_direction": {},
                "first_period": None, "last_period": None}
    by = lambda key: {  # noqa: E731
        k: sum(1 for v in views if getattr(v, key) == k)
        for k in sorted({getattr(v, key) for v in views})
    }
    return {
        "n_disclosed": len(views),
        "by_type": by("type"),
        "by_band": by("band"),
        "by_category": by("category"),
        "by_direction": by("direction"),
        "first_period": min(v.period_start for v in views),
        "last_period": max(v.period_end for v in views),
    }


def public_disclosure_policy() -> dict:
    """The published policy, in one place.

    Built by a function rather than inlined at the one call site because the
    artifact states its policy even when it has no episodes to apply it to:
    "no rows this time" and "no policy" are different facts, and a reader who
    cannot tell them apart has learned nothing from an empty file.
    """
    return {
        "minimum_distinct_actors": PUBLIC_MIN_ACTORS,
        "minimum_distinct_actors_measures": (
            "ambient actor cardinality for the collection and operation "
            "during the interval, not the cardinality of the departure"),
        "excess_dominance_suppression": DOMINANCE_GATE,
        "time_bucket_seconds": PUBLIC_TIME_BUCKET_S,
        "time_coarsening_is_load_bearing": False,
        "exact_statistics_published": False,
        "stable_episode_identifiers_published": False,
        "claim": "disclosure resistance; not anonymity",
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
    if audience == AUDIENCE_PUBLIC:
        src["disclosure_policy"] = public_disclosure_policy()

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

        n_detections = len(rows)
        rows = _latest_per_episode(rows)
        if audience == AUDIENCE_PUBLIC:
            # Collapse repeated episodes that become the same coarse public
            # signal. Publishing their multiplicity would restore a rare
            # temporal signature that coarsening is intended to remove.
            disclosed = {}
            for row in rows:
                view = _public_view(conn, row)
                if view is not None:
                    key = (view.period_start, view.period_end, view.type,
                           view.direction, view.band)
                    disclosed[key] = view
            views = tuple(disclosed[k] for k in sorted(disclosed))
            summary = summarise_public(views)
        else:
            views = tuple(_view(r, audience) for r in rows)
            summary = summarise(views)
            # Local audit output may disclose re-detection counts; the public
            # surface does not disclose suppressed or rare signatures.
            summary["n_detections"] = n_detections
            summary["n_superseded"] = n_detections - len(views)
    finally:
        conn.close()
    proj = SocialProjection(
        audience=audience,
        available=bool(views),
        reason=("" if views else
                ("no episodes satisfy the public disclosure policy"
                 if audience == AUDIENCE_PUBLIC else "no episodes in range")),
        episodes=views,
        summary=summary,
        source=src,
        sink_receipt=sink_receipt,
    )
    if audience == AUDIENCE_PUBLIC:
        assert_identity_free(proj.as_dict())
    return proj
