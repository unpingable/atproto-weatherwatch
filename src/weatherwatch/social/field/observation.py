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


#: What an observation's identity is NOT — the baseline-derived half.
#:
#: `coverage`, `eligible` and `quality` describe the *window*: how much of it
#: was watched and how cleanly. They stay in the identity, because a window
#: re-observed differently is a different observation.
#:
#: These four describe the *baseline it happened to be compared against* at
#: seal time. Re-scoring an unchanged window against a fresher climatology
#: does not make it a different observation of the network; it makes it the
#: same observation with newer evidence attached.
CONFIDENCE_EVIDENCE_KEYS = ("baseline_days", "baseline_n_eff", "support", "note")

#: Same argument, provenance side. `climatology_id` in particular is why one
#: reseal used to mint an entire second copy of the archive.
PROVENANCE_EVIDENCE_KEYS = (
    "run_ids", "climatology_id", "climatology_days", "hour_of_week_supported",
)


def identity_document(document: dict) -> dict:
    """Project a stored observation document onto the fields that identify it.

    One builder, two callers — `observation_id` here and `replay_observation`
    in `run.py` — because an identity computed two ways is two identities. The
    same reason `criteria_table()` exists one level up.

    What survives: the schema, the window and its bounds, what was measured,
    what could not be measured and why, the window's own observation quality,
    the observer, and the statement of structural absences. That last one is
    deliberate and is load-bearing: you cannot strip the record of what this
    instrument cannot see and keep the same identity.

    What is dropped is baseline evidence, which slides on every reseal by
    construction and which the document still carries in full.
    """
    out = {k: v for k, v in document.items() if k != "observation_id"}
    confidence = {k: v for k, v in (out.get("confidence") or {}).items()
                  if k not in CONFIDENCE_EVIDENCE_KEYS}
    out["confidence"] = confidence
    provenance = {k: v for k, v in (out.get("provenance") or {}).items()
                  if k not in PROVENANCE_EVIDENCE_KEYS}
    out["provenance"] = provenance
    return out


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
        """Content address of what was observed, not of what it was scored
        against. See `identity_document`."""
        return receipt_hash(identity_document(self.as_dict(include_id=False)))

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
                      sealed_at: str) -> dict:
    """Seal observations, writing only the ones that actually changed.

    Returns `{"considered", "written", "unchanged"}`. The breakdown is the
    point: on a healthy reseal `written` should be the number of windows that
    arrived, and a run that reports `written == considered` over an unchanged
    range is the archive forking again.

    Once identity stopped hashing over the sliding baseline (see
    `identity_document`), a reseal produced the same ids and the same
    documents, and `INSERT OR REPLACE` still deleted and rewrote all 43,200
    rows every time — about 180 MB of redundant page writes per run, measured.
    That was never *growth*, so it never showed up as a size problem; it was
    just the same work done again, and it is what made a tight producer
    cadence look unaffordable.

    `sealed_at` is deliberately left alone on an unchanged row. It records when
    this observation was sealed, not when it was most recently re-confirmed,
    and the first answer is the true one.
    """
    rows = [
        (o.observation_id, o.ts_start, o.ts_end, o.window,
         o.confidence.support, o.confidence.coverage,
         1 if o.confidence.eligible else 0,
         o.provenance.get("climatology_id", ""),
         stable_json(o.as_dict()), sealed_at)
        for o in obs
    ]
    if not rows:
        return {"considered": 0, "written": 0, "unchanged": 0}

    # One lookup for the whole batch rather than a query per observation.
    existing: dict = {}
    ids = [r[0] for r in rows]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        for oid, document in conn.execute(
                f"SELECT observation_id, document FROM weather_observation "
                f"WHERE observation_id IN ({marks})", chunk):
            existing[oid] = document

    changed = [r for r in rows if existing.get(r[0]) != r[8]]
    if changed:
        conn.executemany(
            "INSERT OR REPLACE INTO weather_observation(observation_id, "
            "ts_start, ts_end, window, support, coverage, eligible, "
            "climatology_id, document, sealed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            changed)
    return {"considered": len(rows), "written": len(changed),
            "unchanged": len(rows) - len(changed)}


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
