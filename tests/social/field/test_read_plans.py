"""The publisher's two reads of the field archive must not scan or sort it.

Every publish asks for the newest climatology and then for the newest 20,000
observations within its fitted range (by time; before 2026-10-19 by the
climatology they were sealed against). On the deployed store (15,526 climatology
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
            (f"o{i}", f"2026-01-01T00:{i:02d}:00+00:00", "e", "60s", "thin",
             1.0, 1, "c1" if i % 2 else "c0", f'{{"i": {i}}}', "s"))
    assert fobs.load_climatology(conn)["climatology_id"] == "c1"
    docs, total = fobs.load_observations(conn, climatology_id="c1", limit=5)
    assert total == 15
    assert [d["i"] for d in docs] == [21, 23, 25, 27, 29]


def _no_table_scan(plan):
    return not any(step.startswith("SCAN weather_observation") and
                   "INDEX" not in step for step in plan)


def test_observations_by_time_come_back_in_index_order(tmp_path):
    conn = _conn(tmp_path)
    plan = _plan(conn, "SELECT o.document FROM weather_observation AS o "
                       "WHERE o.ts_start >= ? AND o.rowid = (SELECT MAX(i.rowid) "
                       "FROM weather_observation AS i WHERE i.ts_start = o.ts_start "
                       "AND i.window = o.window) ORDER BY o.ts_start DESC LIMIT ?",
                 ("2026-01-01T00:00:00+00:00", 20_000))
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert _no_table_scan(plan), plan
    assert any("idx_obs_time" in step for step in plan), plan


def test_the_window_count_by_time_is_answered_from_the_index(tmp_path):
    """The review of 2026-10-19 caught `COUNT(*) WHERE ts_end >= ?`: a full
    table scan over ~1.2 GB of documents on every publish. The count must
    read the ts_start index and nothing else."""
    conn = _conn(tmp_path)
    plan = _plan(conn, "SELECT COUNT(DISTINCT ts_start) FROM weather_observation "
                       "WHERE ts_start >= ?", ("2026-01-01T00:00:00+00:00",))
    assert _no_table_scan(plan), plan
    assert any("COVERING INDEX idx_obs_time" in step for step in plan), plan


def test_a_window_stored_twice_is_read_once_newest_first(tmp_path):
    """A window re-observed differently (a collector restart splitting its
    bucket) has two rows under two identities. The time read returns the
    newest row per window and counts windows; the archive read by
    climatology_id still returns rows as stored."""
    conn = _conn(tmp_path)
    for i, (oid, clim, doc) in enumerate((("old", "c0", '{"v": "old"}'),
                                          ("new", "c1", '{"v": "new"}'))):
        conn.execute(
            "INSERT INTO weather_observation VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, "2026-01-01T00:05:00+00:00", "2026-01-01T00:05:59+00:00",
             "60s", "thin", 1.0 if i else 0.5, 1, clim, doc, f"s{i}"))
    conn.execute(
        "INSERT INTO weather_observation VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("other", "2026-01-01T00:06:00+00:00", "2026-01-01T00:06:59+00:00",
         "60s", "thin", 1.0, 1, "c1", '{"v": "other"}', "s2"))
    docs, total = fobs.load_observations(conn, since="2026-01-01T00:00:00Z")
    assert total == 2
    assert [d["v"] for d in docs] == ["new", "other"]
    rows, total = fobs.load_observations(conn, climatology_id="c0")
    assert total == 1 and rows[0]["v"] == "old"


def test_the_time_reader_returns_the_newest_rows_in_range(tmp_path):
    conn = _conn(tmp_path)
    for i in range(30):
        conn.execute(
            "INSERT INTO weather_observation VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"o{i}", f"2026-01-01T00:{i:02d}:00Z", f"2026-01-01T00:{i:02d}:59Z",
             "60s", "thin", 1.0, 1, "c0", f'{{"i": {i}}}', "s"))
    docs, total = fobs.load_observations(conn, since="2026-01-01T00:10:00Z",
                                         limit=5)
    assert total == 20, "a Z bound must not drop the window that starts on it"
    assert [d["i"] for d in docs] == [25, 26, 27, 28, 29]
