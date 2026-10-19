"""What makes a field observation *that* observation, and what merely scored it.

These tests exist because of a measured production incident. The sealer re-fits
its climatology over a trailing range, so `ts_start`/`ts_end` move on every run
by construction. `climatology_id` hashed over them, `observe()` stamps that id
onto every observation, and the observation hashed over *that* — so one hourly
reseal minted 43,201 logically identical observations under fresh ids. Two runs
took the live store from 474 MB to 827 MB. Hourly, that was roughly 4 GB a day
of duplicate epistemology.

The repair is at the identity layer, not the retention layer: a producer that
manufactures logical duplicates is broken whether or not something downstream
deletes them efficiently.

The guarantee, restated as tests: **resealing unchanged data is a no-op, and
resealing after time advances adds only the windows that actually arrived.**
"""

from __future__ import annotations

import json
import re

import pytest

from weatherwatch import db, query
from weatherwatch.social import store as social_store
from weatherwatch.social.field import climatology as clim_mod
from weatherwatch.social.field import observation as fobs
from weatherwatch.social.field import run as frun
from tests.social.field.conftest import BASE, HOUR, ENDPOINT, write_hourly_run

IDENTITY_RE = re.compile(
    r"did:(plc|web|key):|at://|bafy[a-z0-9]{10,}"
    r"|[a-z0-9-]+\.bsky\.(social|app)|\ba:[0-9a-f]{12}\b")


@pytest.fixture()
def sealed(tmp_path):
    """One database, sealed repeatedly over a sliding range — the live shape.

    Built here rather than borrowed from `tests/social/field/conftest.py`,
    whose fixtures are scoped to that directory.
    """
    conn = db.connect(tmp_path / "field.sqlite")
    db.init_db(conn)
    write_hourly_run(conn, "run-a", n_days=10)
    yield conn
    conn.close()


def _build(conn, *, hours: int):
    """Fit and seal over `[BASE, BASE + hours)`, as a trailing range would."""
    runs = query.compatible_runs(conn, ENDPOINT)
    until_us = (BASE + hours * HOUR) * 1_000_000
    return frun.build_all(conn, runs, since_us=BASE * 1_000_000,
                          until_us=until_us, endpoint=ENDPOINT)


def _ids(observations) -> dict:
    return {o.ts_start: o.observation_id for o in observations}


# --- 1. resealing unchanged data is a no-op --------------------------------

def test_two_seals_of_the_same_data_are_byte_identical(sealed):
    _p, clim_a, obs_a, _c = _build(sealed, hours=192)
    _p, clim_b, obs_b, _c = _build(sealed, hours=192)
    assert clim_a.climatology_id == clim_b.climatology_id
    assert _ids(obs_a) == _ids(obs_b)


def test_resealing_unchanged_data_adds_no_rows(sealed, tmp_path):
    """The regression guard, at the layer the incident was measured in."""
    conn = social_store.connect(tmp_path / "social.sqlite")
    social_store.init_db(conn)
    fobs.init(conn)

    def seal(hours):
        _p, clim, obs, _c = _build(sealed, hours=hours)
        fobs.save_climatology(conn, clim, "t")
        fobs.save_observations(conn, obs, "t")
        conn.commit()
        return conn.execute(
            "SELECT COUNT(*) FROM weather_observation").fetchone()[0]

    first = seal(192)
    for _ in range(4):
        assert seal(192) == first, "a reseal forked the archive"


def test_the_climatology_table_does_not_grow_on_reseal(sealed, tmp_path):
    conn = social_store.connect(tmp_path / "social.sqlite")
    social_store.init_db(conn)
    fobs.init(conn)
    for _ in range(5):
        _p, clim, _obs, _c = _build(sealed, hours=192)
        fobs.save_climatology(conn, clim, "t")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM weather_climatology").fetchone()[0] == 1


# --- 2. advancing adds only what arrived -----------------------------------

def test_advancing_one_window_produces_exactly_one_new_observation(sealed):
    _p, _clim, before, _c = _build(sealed, hours=192)
    _p, _clim, after, _c = _build(sealed, hours=193)

    a, b = _ids(before), _ids(after)
    carried = {t for t in a if t in b and a[t] == b[t]}
    forked = {t for t in a if t in b and a[t] != b[t]}
    added = set(b) - set(a)

    assert len(added) == 1, f"expected one new window, got {len(added)}"
    assert forked == set(), f"{len(forked)} unchanged windows were re-minted"
    assert carried == set(a), "an existing observation lost its identity"


def test_advancing_a_day_adds_a_day(sealed):
    _p, _clim, before, _c = _build(sealed, hours=192)
    _p, _clim, after, _c = _build(sealed, hours=216)
    a, b = _ids(before), _ids(after)
    assert len(set(b) - set(a)) == 24
    assert all(a[t] == b[t] for t in a), "existing observations forked"


def test_growth_is_bounded_by_arrivals_not_by_archive_size(sealed, tmp_path):
    """Steady state: each reseal costs the windows that arrived, and nothing
    proportional to how much history is already sealed."""
    conn = social_store.connect(tmp_path / "social.sqlite")
    social_store.init_db(conn)
    fobs.init(conn)
    counts = []
    for hours in (192, 200, 208, 216):
        _p, clim, obs, _c = _build(sealed, hours=hours)
        fobs.save_climatology(conn, clim, "t")
        fobs.save_observations(conn, obs, "t")
        conn.commit()
        counts.append(conn.execute(
            "SELECT COUNT(*) FROM weather_observation").fetchone()[0])
    deltas = [b - a for a, b in zip(counts, counts[1:])]
    assert deltas == [8, 8, 8], f"growth tracked the archive, not arrivals: {deltas}"


# --- 3. genuinely different baselines still differ -------------------------

def test_a_real_change_in_the_baseline_changes_its_identity(sealed):
    """Stability must not become blindness."""
    _p, short, _o, _c = _build(sealed, hours=48)
    _p, long_, _o, _c = _build(sealed, hours=192)
    assert short.climatology_id != long_.climatology_id


def test_an_empty_baseline_is_not_a_fitted_one(sealed):
    _p, clim, _o, _c = _build(sealed, hours=192)
    empty = clim_mod.build([], window=clim.window)
    assert clim.climatology_id != empty.climatology_id


def test_a_different_observer_is_a_different_baseline(sealed):
    _p, clim, _o, _c = _build(sealed, hours=192)
    runs = query.compatible_runs(sealed, ENDPOINT)
    other = frun.build_all(
        sealed, runs, since_us=BASE * 1_000_000,
        until_us=(BASE + 192 * HOUR) * 1_000_000,
        endpoint="wss://somewhere-else.invalid/subscribe")[1]
    assert clim.climatology_id != other.climatology_id, (
        "the observer is part of what a baseline is")


def test_changed_metrics_change_the_observation(sealed):
    _p, _c, obs, _ = _build(sealed, hours=192)
    original = obs[0]
    moved = fobs.SocialWeatherObservation(
        **{**{f: getattr(original, f) for f in
              ("schema_version", "ts_start", "ts_end", "window", "unavailable",
               "confidence", "provenance", "non_claims")},
           "metrics": {**original.metrics, "interaction_velocity": 999.0}})
    assert moved.observation_id != original.observation_id


# --- 4. the extent survives as evidence ------------------------------------

def test_the_range_is_still_published_even_though_it_is_not_identity(sealed):
    _p, clim, _o, _c = _build(sealed, hours=192)
    document = clim.as_dict()
    for field in clim_mod.EXTENT_FIELDS:
        assert field in document, f"{field} was dropped from the document"
    assert document["ts_start"] < document["ts_end"]
    assert document["n_windows"] > 0 and document["n_days"] > 0


def test_two_baselines_with_one_id_still_report_their_own_extents(sealed):
    """Identity is stable across the slide; the evidence is not, and says so."""
    _p, a, _o, _c = _build(sealed, hours=192)
    _p, b, _o, _c = _build(sealed, hours=192)
    assert a.climatology_id == b.climatology_id
    assert a.as_dict()["ts_end"] == b.as_dict()["ts_end"]


def test_an_observation_still_carries_the_baseline_it_was_scored_against(sealed):
    _p, clim, obs, _c = _build(sealed, hours=192)
    document = obs[-1].as_dict()
    assert document["provenance"]["climatology_id"] == clim.climatology_id
    for key in ("baseline_days", "support"):
        assert key in document["confidence"]


def test_rescoring_updates_the_evidence_without_moving_the_identity(sealed):
    """The same window, scored against a longer baseline, is the same
    observation carrying newer evidence."""
    _p, _c, early, _ = _build(sealed, hours=192)
    _p, _c, later, _ = _build(sealed, hours=216)
    by_start = {o.ts_start: o for o in later}
    first = early[0]
    same = by_start[first.ts_start]
    assert same.observation_id == first.observation_id
    assert same.as_dict()["provenance"]["climatology_id"] != \
        first.as_dict()["provenance"]["climatology_id"]


# --- 5. replay, precision and privacy are unchanged ------------------------

def test_replay_still_reproduces_every_stored_id(sealed):
    _p, _c, obs, _ = _build(sealed, hours=192)
    for o in obs[:200]:
        assert frun.replay_observation(o.as_dict()) == o.observation_id


def test_replay_uses_the_same_projection_as_minting(sealed):
    """Two ways to compute one identity is two identities."""
    _p, _c, obs, _ = _build(sealed, hours=192)
    document = obs[0].as_dict()
    stripped = fobs.identity_document(document)
    assert "climatology_id" not in stripped["provenance"]
    assert "support" not in stripped["confidence"]
    assert frun.replay_observation(document) == document["observation_id"]


def test_structural_absences_still_cannot_be_stripped(sealed):
    """DECISIONS section 2: the statement of limits is content, not
    presentation, and removing it must break the identity."""
    _p, _c, obs, _ = _build(sealed, hours=192)
    document = obs[0].as_dict()
    without = dict(document)
    without.pop("structural_absences")
    assert frun.replay_observation(without) != document["observation_id"]


def test_the_window_quality_still_identifies_the_observation(sealed):
    """Coverage and quality describe the window, not the baseline, so they
    stay in the identity: a window re-observed differently is different."""
    _p, _c, obs, _ = _build(sealed, hours=192)
    document = obs[0].as_dict()
    degraded = json.loads(json.dumps(document))
    degraded["confidence"]["coverage"] = 0.25
    assert frun.replay_observation(degraded) != document["observation_id"]


def test_identity_change_added_no_field_and_no_identifier(sealed):
    _p, clim, obs, _c = _build(sealed, hours=192)
    payload = json.dumps([o.as_dict() for o in obs[:50]] + [clim.as_dict()])
    assert not IDENTITY_RE.search(payload)
    document = obs[0].as_dict()
    assert set(document) == {
        "schema_version", "ts_start", "ts_end", "window", "metrics",
        "unavailable", "confidence", "provenance", "non_claims",
        "structural_absences", "observation_id"}


def test_timestamps_are_no_finer_than_before(sealed):
    _p, _c, obs, _ = _build(sealed, hours=192)
    for o in obs[:50]:
        assert o.ts_start.endswith("Z") or "+00:00" in o.ts_start
        assert "." not in o.ts_start, "sub-second precision appeared"


# --- 5. sealing memory is bounded by a batch, not by the range -------------

def _many(observations, n):
    """`n` distinct observations, by sliding real ones along the clock."""
    import dataclasses
    from weatherwatch import timeutil

    out = []
    for i in range(n):
        o = observations[i % len(observations)]
        start = timeutil.to_epoch(o.ts_start) + (i // len(observations)) * 10**7
        out.append(dataclasses.replace(
            o, ts_start=timeutil.us_to_iso(int(start * 1_000_000))))
    return out


def test_sealing_memory_does_not_scale_with_the_range(sealed, tmp_path):
    """Measured on a synthetic 30-day minute store: sealing 43,205 windows
    built every document and then read every stored one back before writing
    any, holding two copies of the range at once. Both are per-batch now, so
    tripling the range must not triple the peak."""
    import tracemalloc

    _p, _clim, base, _c = _build(sealed, hours=192)

    def peak_for(n):
        obs = _many(base, n)
        conn = social_store.connect(tmp_path / f"social-{n}.sqlite")
        social_store.init_db(conn)
        fobs.init(conn)
        fobs.save_observations(conn, obs, "t")
        rescored = [dataclasses_replace_provenance(o) for o in obs]
        tracemalloc.start()
        result = fobs.save_observations(conn, rescored, "t2")
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        conn.close()
        assert result == {"considered": n, "written": n, "unchanged": 0}
        return peak

    small, large = peak_for(2_000), peak_for(6_000)
    assert large < 1.5 * small, (
        f"peak grew with the range: {small} bytes at 2,000 windows, "
        f"{large} at 6,000")


def dataclasses_replace_provenance(o):
    """Same observation, newer baseline evidence: the every-run reseal shape."""
    import dataclasses
    return dataclasses.replace(
        o, provenance={**o.provenance, "climatology_id": "newer"})


def test_batched_sealing_writes_exactly_what_changed(sealed, tmp_path):
    _p, clim, obs, _c = _build(sealed, hours=192)
    obs = _many(obs, 1_234)
    conn = social_store.connect(tmp_path / "social.sqlite")
    social_store.init_db(conn)
    fobs.init(conn)
    assert fobs.save_observations(conn, obs, "t") == {
        "considered": 1_234, "written": 1_234, "unchanged": 0}
    mixed = [dataclasses_replace_provenance(o) if i % 3 == 0 else o
             for i, o in enumerate(obs)]
    assert fobs.save_observations(conn, mixed, "t2") == {
        "considered": 1_234, "written": 412, "unchanged": 822}
    stored = dict(conn.execute(
        "SELECT observation_id, document FROM weather_observation"))
    from weatherwatch.social.envelope import stable_json
    assert stored == {o.observation_id: stable_json(o.as_dict()) for o in mixed}


def test_series_points_share_their_repeated_values(sealed):
    """Fourteen 30-day minute series are ~600,000 points held at once by the
    sealer. Their flags, coverage state and run id take a handful of values,
    so one object per value is shared rather than one per point."""
    from tests.social.field.conftest import BASE as base, HOUR as hour

    runs = query.compatible_runs(sealed, ENDPOINT)
    s = query.series(sealed, runs, "like.create", since=base - 24 * hour,
                     until=base + 240 * hour)
    for attr in ("flags", "coverage_state", "run_id"):
        values = [getattr(p, attr) for p in s.points]
        assert len({id(v) for v in values}) == len(set(values)), attr
    assert any(p.count is None for p in s.points), "fixture has no gap"
