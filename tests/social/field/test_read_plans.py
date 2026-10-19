"""The publisher's two reads of the field archive must not scan or sort it.

Every publish asks for the newest climatology and then for the newest 20,000
observations scored against it. On the deployed store (15,526 climatology
documents of ~30 KB, 421,295 observations of ~2.9 KB) the first was a full
table scan plus a temp B-tree -- roughly 470 MB of pages read to return one
row -- and the second pushed every matching ~3 KB document through a sort.
Both are answerable straight from an index, with identical results.
"""

from __future__ import annotations

from weatherwatch.social import store
from weatherwatch.social.field import observation as fobs


def _plan(conn, sql, params=()):
    return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _conn(tmp_path):
    conn = store.connect(tmp_path / "social.sqlite")
    store.init_db(conn)
    fobs.init(conn)
    return conn


def test_newest_climatology_is_read_from_an_index(tmp_path):
    conn = _conn(tmp_path)
    plan = _plan(conn, "SELECT document FROM weather_climatology "
                       "ORDER BY sealed_at DESC LIMIT 1")
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert not any(step.startswith("SCAN weather_climatology") and
                   "INDEX" not in step for step in plan), plan


def test_observations_for_one_baseline_come_back_in_index_order(tmp_path):
    conn = _conn(tmp_path)
    plan = _plan(conn, "SELECT document FROM weather_observation "
                       "WHERE climatology_id = ? ORDER BY ts_start DESC "
                       "LIMIT ?", ("x", 20_000))
    assert not any("TEMP B-TREE" in step for step in plan), plan


def test_the_readers_still_return_the_newest_rows(tmp_path):
    conn = _conn(tmp_path)
    for i, sealed in enumerate(("2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z",
                                "2026-01-02T00:00:00Z")):
        conn.execute(
            "INSERT INTO weather_climatology VALUES (?,?,?,?,?,?,?,?)",
            (f"c{i}", "a", "b", "60s", 1, 1, f'{{"climatology_id": "c{i}"}}',
             sealed))
    for i in range(30):
        conn.execute(
            "INSERT INTO weather_observation VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"o{i}", f"2026-01-01T00:{i:02d}:00Z", "e", "60s", "thin", 1.0,
             1, "c1" if i % 2 else "c0", f'{{"i": {i}}}', "s"))
    assert fobs.load_climatology(conn)["climatology_id"] == "c1"
    docs, total = fobs.load_observations(conn, climatology_id="c1", limit=5)
    assert total == 15
    assert [d["i"] for d in docs] == [21, 23, 25, 27, 29]
