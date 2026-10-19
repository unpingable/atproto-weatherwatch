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


def _baseline_evidence(clim: dict | None, hour: int) -> tuple[dict, dict]:
    """The baseline evidence a window carries when scored against `clim`.

    `clim` is a climatology *document* (`Climatology.as_dict()` or the row
    `load_climatology` returns). One derivation for both the sealer and the
    reader: `observe()` stamps this onto a fresh observation, and `rescore()`
    re-derives it for a stored one, so the two cannot disagree about what a
    baseline says for an hour. Returns `(confidence_fields,
    provenance_fields)`, exactly the keys in `CONFIDENCE_EVIDENCE_KEYS` and
    `PROVENANCE_EVIDENCE_KEYS` minus `run_ids`, which is seal-time
    provenance and not baseline evidence.
    """
    baseline_days, n_eff, support, note = 0, None, UNSUPPORTED, (
        "No climatology supplied; this observation is unconditioned.")
    provenance: dict = {}
    if clim is not None:
        ref = (clim.get("quantities") or {}).get("interaction_velocity")
        if ref is not None:
            cell = next((c for c in ref.get("diurnal", ())
                         if c.get("hour") == hour), None)
            baseline_days = cell["n_days"] if cell else 0
            n_eff = (ref.get("overall") or {}).get("n_eff")
            support = ref.get("support", UNSUPPORTED)
            note = ref.get("support_note", "")
        provenance = {
            "climatology_id": clim.get("climatology_id"),
            "climatology_days": clim.get("n_days"),
            "hour_of_week_supported": clim.get("hour_of_week_supported"),
        }
    confidence = {
        "baseline_days": baseline_days,
        "baseline_n_eff": (round(n_eff, 2) if n_eff is not None else None),
        "support": support,
        "note": note,
    }
    return confidence, provenance


def rescore(documents: list, climatology: dict | None) -> list:
    """Stored observations re-scored against `climatology`, the document the
    reader is about to compare them with.

    An observation is stored once, with the baseline evidence it was sealed
    with (DECISIONS.md §8). The page still has to show every window against
    the *current* baseline, so the reader re-derives that evidence here
    instead of the sealer rewriting the archive. Identity is untouched by
    construction: every key written is in `CONFIDENCE_EVIDENCE_KEYS` or
    `PROVENANCE_EVIDENCE_KEYS`, which `identity_document` drops, and that is
    asserted below. Returns new dicts; the stored documents stay as sealed.
    """
    out = []
    for document in documents:
        hour = _hour_of_window(document.get("ts_start"))
        confidence, provenance = _baseline_evidence(climatology, hour)
        assert set(confidence) <= set(CONFIDENCE_EVIDENCE_KEYS)
        assert set(provenance) <= set(PROVENANCE_EVIDENCE_KEYS)
        rescored = dict(document)
        rescored["confidence"] = {**(document.get("confidence") or {}),
                                  **confidence}
        rescored["provenance"] = {**(document.get("provenance") or {}),
                                  **provenance}
        out.append(rescored)
    return out


def _hour_of_window(ts_start: str | None) -> int:
    import datetime
    try:
        return datetime.datetime.fromisoformat(
            (ts_start or "").replace("Z", "+00:00")).hour
    except ValueError:
        return -1


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
    # The same derivation the reader uses to re-score a stored window. The
    # climatology document is built once per seal, not once per window:
    # `as_dict()` serialises every distribution and `climatology_id` hashes
    # them, so per-window calls cost minutes over a month of minute windows.
    evidence, baseline_prov = _baseline_evidence(
        _climatology_document(clim) if clim is not None else None, hour)

    nominal = point.bucket_width or 1
    prov = dict(provenance or {})
    prov.update(baseline_prov)

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
            **evidence,
        ),
        provenance=prov,
        non_claims=non_claims_for(sorted(metrics)),
    )


def _climatology_document(clim: Climatology) -> dict:
    """`clim.as_dict()`, computed once per `Climatology` instance.

    A `Climatology` is a frozen dataclass built once per seal; the document
    is cached on it the same way `climatology_id` is (through `__dict__`,
    which is what works on a frozen dataclass).
    """
    cached = clim.__dict__.get("_document")
    if cached is None:
        cached = clim.as_dict()
        clim.__dict__["_document"] = cached
    return cached


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
-- The publisher's two reads, answered from an index instead of a scan and a
-- sort over multi-kilobyte documents. Additive and idempotent: an existing
-- store gains them on its next `init`.
CREATE INDEX IF NOT EXISTS idx_clim_sealed ON weather_climatology(sealed_at);
CREATE INDEX IF NOT EXISTS idx_obs_clim_time
    ON weather_observation(climatology_id, ts_start);
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


#: Observations built, compared and written per batch. Also the size of the
#: `IN (...)` lookup, which keeps it under SQLite's bound-parameter limit.
SEAL_BATCH = 500


def save_observations(conn: sqlite3.Connection, obs: list,
                      sealed_at: str) -> dict:
    """Seal observations, writing only the ones not already stored.

    **An observation is stored once, with the baseline evidence it was sealed
    with.** The climatology is separately versioned derived state: a refit
    moves `climatology_id` on essentially every run, and before 2026-10-19
    that moved the baseline evidence inside every stored document, so a run
    that advanced by five windows rewrote all ~43,200 rows of the 30-day
    range (~215 MB of write-ahead log, measured; DECISIONS.md §8). The
    reader now re-derives baseline evidence against the newest climatology at
    render (`rescore`), so the stored document is the archive and the page
    stays current without the archive being rewritten.

    Returns `{"considered", "written", "unchanged"}`. On a healthy run
    `written` is the number of windows that arrived, and a run that reports
    `written == considered` over an unchanged range is the archive forking
    again.

    `sealed_at` records when an observation was sealed, not when it was most
    recently re-confirmed; an existing row is never touched.

    Works in batches of `SEAL_BATCH`: each batch looks up its own ids and
    builds documents only for the ones to write, so memory does not scale
    with the range. The caller's transaction still spans the whole call.
    """
    considered = written = 0
    for start in range(0, len(obs), SEAL_BATCH):
        batch = obs[start:start + SEAL_BATCH]
        ids = [o.observation_id for o in batch]
        marks = ",".join("?" * len(ids))
        existing = {r[0] for r in conn.execute(
            f"SELECT observation_id FROM weather_observation "
            f"WHERE observation_id IN ({marks})", ids)}
        rows = []
        for o, observation_id in zip(batch, ids):
            if observation_id in existing:
                continue
            document = o.as_dict(include_id=False)
            document["observation_id"] = observation_id
            rows.append((
                observation_id, o.ts_start, o.ts_end, o.window,
                o.confidence.support, o.confidence.coverage,
                1 if o.confidence.eligible else 0,
                o.provenance.get("climatology_id", ""),
                stable_json(document), sealed_at))
        if rows:
            conn.executemany(
                "INSERT INTO weather_observation(observation_id, "
                "ts_start, ts_end, window, support, coverage, eligible, "
                "climatology_id, document, sealed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows)
        considered += len(batch)
        written += len(rows)
    return {"considered": considered, "written": written,
            "unchanged": considered - written}


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

    The publisher reads by time and then `rescore`s what it loads against
    the newest climatology, so every window on a page is compared with one
    baseline. `climatology_id` instead restricts to observations *sealed*
    against one baseline, which is a question about the archive, not about
    current conditions.
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
