"""`SocialWeatherObservation` -- the record a weather station actually files.

Not a detection. `DetectionEnvelope` says *something departed*; this says
*here were the conditions*, which is the more basic record and the one that
has to exist first. Sealing them as separate types is deliberate: an
observation is filed for every window including the entirely ordinary ones,
and collapsing "nothing unusual" into the same object as "something happened"
is how an archive quietly becomes a list of incidents.

Four things ride on every observation, and the last two are the point:

    metrics      what was measured
    confidence   how much weight it can carry, including when the answer is
                 "not much" -- coverage, effective sample size, and how many
                 independent days the conditioning baseline actually had
    provenance   endpoint, runs, code and config versions, the climatology it
                 was scored against
    non_claims   what this record does not say, assembled from the quantities
                 present rather than boilerplate

`unavailable` is a first-class field. A quantity that could not be measured
appears there with the reason, rather than being absent or nulled -- absence
of a field is a design property here, and design properties should be legible
in the data, not just the docs.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field

from ... import timeutil
from ..envelope import receipt_hash, stable_json
from . import FIELD_SCHEMA_VERSION
from .climatology import Climatology, UNSUPPORTED
from .quantities import QUANTITY_BY_NAME, FieldPoint, non_claims_for

#: Reasons a quantity may be unmeasurable in a given window.
UNAVAILABLE_UNOBSERVED = "window not observed by the collector"
UNAVAILABLE_NO_DENOMINATOR = "denominator was zero in this window"
UNAVAILABLE_NO_CONTEXT = "insufficient trailing eligible context"

#: Things this instrument structurally cannot measure, stated on every record.
#: These are not gaps awaiting work -- each is absent because measuring it
#: would require retention the instrument refuses.
STRUCTURAL_ABSENCES = {
    "participants": "No count of distinct people. The counters aggregate "
                    "events, not actors, so any per-participant quantity "
                    "(including newcomer ratio) is unavailable by "
                    "construction rather than unimplemented.",
    "location": "No geography. ATProto exposes none, and inferring it from "
                "PDS or IP data would describe infrastructure while implying "
                "people.",
    "content": "No text, topic or sentiment. Record bodies are discarded at "
               "the classifier and never stored.",
    "identity": "No actor, target, handle or record key.",
}


@dataclass(frozen=True)
class Confidence:
    """How much this observation can carry. Designed to be able to say 'little'."""

    coverage: float               # observed seconds / nominal window seconds
    eligible: bool                # did it qualify as baseline-grade
    quality: str                  # the weather lane's own window quality
    baseline_days: int            # independent replicates behind the hour cell
    baseline_n_eff: float | None  # after AR(1) correction
    support: str                  # supported | thin | unsupported
    note: str = ""


@dataclass(frozen=True)
class SocialWeatherObservation:
    schema_version: int
    ts_start: str
    ts_end: str
    window: str
    metrics: dict
    unavailable: dict
    confidence: Confidence
    provenance: dict
    non_claims: tuple

    @property
    def observation_id(self) -> str:
        return receipt_hash(self.as_dict(include_id=False))

    def as_dict(self, include_id: bool = True) -> dict:
        d = {
            "schema_version": self.schema_version,
            "ts_start": self.ts_start,
            "ts_end": self.ts_end,
            "window": self.window,
            "metrics": {k: self.metrics[k] for k in sorted(self.metrics)},
            "unavailable": {k: self.unavailable[k]
                            for k in sorted(self.unavailable)},
            "confidence": asdict(self.confidence),
            "provenance": {k: self.provenance[k]
                           for k in sorted(self.provenance)},
            "non_claims": list(self.non_claims),
            "structural_absences": STRUCTURAL_ABSENCES,
        }
        if include_id:
            d["observation_id"] = self.observation_id
        return d


def observe(
    point: FieldPoint,
    clim: Climatology | None,
    provenance: dict | None = None,
) -> SocialWeatherObservation:
    """Seal one window of the field as an observation."""
    import datetime

    metrics: dict = {}
    unavailable: dict = {}
    for name, value in point.values.items():
        if value is None:
            if not point.observed:
                unavailable[name] = UNAVAILABLE_UNOBSERVED
            elif QUANTITY_BY_NAME[name].context_windows:
                unavailable[name] = UNAVAILABLE_NO_CONTEXT
            else:
                unavailable[name] = UNAVAILABLE_NO_DENOMINATOR
        else:
            metrics[name] = round(float(value), 6)

    hour = datetime.datetime.fromtimestamp(
        point.bucket_start, tz=datetime.timezone.utc).hour
    baseline_days, n_eff, support, note = 0, None, UNSUPPORTED, (
        "No climatology supplied; this observation is unconditioned.")
    if clim is not None:
        ref = next((q for n, q in sorted(clim.quantities.items())
                    if n == "interaction_velocity"), None)
        if ref is not None:
            cell = next((c for c in ref.diurnal if c.hour == hour), None)
            baseline_days = cell.n_days if cell else 0
            n_eff = ref.overall.n_eff
            support = ref.support
            note = ref.support_note

    nominal = point.bucket_width or 1
    prov = dict(provenance or {})
    if clim is not None:
        prov["climatology_id"] = clim.climatology_id
        prov["climatology_days"] = clim.n_days
        prov["hour_of_week_supported"] = clim.hour_of_week_supported

    return SocialWeatherObservation(
        schema_version=FIELD_SCHEMA_VERSION,
        ts_start=timeutil.us_to_iso(point.bucket_start * 1_000_000),
        ts_end=timeutil.us_to_iso(
            (point.bucket_start + point.bucket_width) * 1_000_000),
        window=f"{point.bucket_width}s",
        metrics=metrics,
        unavailable=unavailable,
        confidence=Confidence(
            coverage=round(min(point.observed_seconds / nominal, 1.0), 4),
            eligible=point.eligible,
            quality=point.quality,
            baseline_days=baseline_days,
            baseline_n_eff=(round(n_eff, 2) if n_eff is not None else None),
            support=support,
            note=note,
        ),
        provenance=prov,
        non_claims=non_claims_for(sorted(metrics)),
    )


# --- storage ---------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_observation (
    observation_id  TEXT PRIMARY KEY,
    ts_start        TEXT NOT NULL,
    ts_end          TEXT NOT NULL,
    window          TEXT NOT NULL,
    support         TEXT NOT NULL,
    coverage        REAL NOT NULL,
    eligible        INTEGER NOT NULL,
    climatology_id  TEXT NOT NULL,
    document        TEXT NOT NULL,
    sealed_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weather_climatology (
    climatology_id  TEXT PRIMARY KEY,
    ts_start        TEXT NOT NULL,
    ts_end          TEXT NOT NULL,
    window          TEXT NOT NULL,
    n_days          INTEGER NOT NULL,
    n_eligible      INTEGER NOT NULL,
    document        TEXT NOT NULL,
    sealed_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_time ON weather_observation(ts_start);
CREATE INDEX IF NOT EXISTS idx_obs_clim ON weather_observation(climatology_id);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def save_climatology(conn: sqlite3.Connection, clim: Climatology,
                     sealed_at: str) -> str:
    doc = clim.as_dict()
    conn.execute(
        "INSERT OR REPLACE INTO weather_climatology(climatology_id, ts_start, "
        "ts_end, window, n_days, n_eligible, document, sealed_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (clim.climatology_id, clim.ts_start, clim.ts_end, clim.window,
         clim.n_days, clim.n_eligible, stable_json(doc), sealed_at),
    )
    return clim.climatology_id


def save_observations(conn: sqlite3.Connection, obs: list,
                      sealed_at: str) -> int:
    rows = [
        (o.observation_id, o.ts_start, o.ts_end, o.window,
         o.confidence.support, o.confidence.coverage,
         1 if o.confidence.eligible else 0,
         o.provenance.get("climatology_id", ""),
         stable_json(o.as_dict()), sealed_at)
        for o in obs
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO weather_observation(observation_id, ts_start, "
        "ts_end, window, support, coverage, eligible, climatology_id, "
        "document, sealed_at) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_observations(conn: sqlite3.Connection, since: str | None = None,
                      until: str | None = None, limit: int = 20_000,
                      climatology_id: str | None = None
                      ) -> tuple[list, int]:
    """The most recent `limit` observations, oldest-first, plus the total.

    Ordering ascending and then LIMITing takes the *oldest* rows, which is
    almost never what a weather station wants and is invisible once rendered:
    the live page reported conditions a week stale because the store held more
    than the limit and the newest rows fell off the end. Select descending,
    then reverse.

    Returns the total as well, so the caller can disclose a truncation rather
    than present a partial archive as a whole one.

    `climatology_id` restricts to observations scored against one baseline. An
    observation only means anything relative to the climatology it was scored
    against, so mixing runs on one page would put readings from a four-day
    baseline beside readings from a fortnight's and call them comparable.
    """
    import json

    clauses, params = [], []
    if climatology_id:
        clauses.append("climatology_id = ?")
        params.append(climatology_id)
    if since:
        clauses.append("ts_end >= ?")
        params.append(since)
    if until:
        clauses.append("ts_start <= ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM weather_observation{where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT document FROM weather_observation{where} "
        "ORDER BY ts_start DESC LIMIT ?", params + [limit]).fetchall()
    docs = [json.loads(r[0]) for r in reversed(rows)]
    return docs, total


def load_climatology(conn: sqlite3.Connection,
                     climatology_id: str | None = None) -> dict | None:
    import json
    if climatology_id:
        row = conn.execute(
            "SELECT document FROM weather_climatology WHERE climatology_id=?",
            (climatology_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT document FROM weather_climatology "
            "ORDER BY sealed_at DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None
