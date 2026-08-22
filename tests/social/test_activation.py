"""Activation: off unless configured, and both states leave a receipt."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from weatherwatch import query
from weatherwatch.collector import Collector
from weatherwatch.social import config, store
from weatherwatch.social.sink import SocialSink

from .conftest import BASE_US
from .test_sink_integration import ENDPOINT, _drive, _msgs


# --- default is off ---------------------------------------------------------

def test_empty_environment_is_off():
    cfg = config.from_env({})
    assert cfg.enabled is False
    assert cfg.source == "default"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "enabled"])
def test_affirmative_values_enable(raw):
    assert config.from_env({config.ENV_ENABLED: raw}).enabled is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "disabled"])
def test_negative_values_leave_it_off(raw):
    assert config.from_env({config.ENV_ENABLED: raw}).enabled is False


@pytest.mark.parametrize("raw", ["maybe", "2", "sure", "on/off", "y"])
def test_ambiguous_values_are_refused_not_guessed(raw):
    """An ambiguous value here decides whether identity is retained. The one
    thing this must never do is pick a side quietly."""
    with pytest.raises(config.ConfigError):
        config.from_env({config.ENV_ENABLED: raw})


def test_merely_setting_other_vars_does_not_enable():
    cfg = config.from_env({
        config.ENV_DB: "/tmp/x.sqlite",
        config.ENV_COLLECTIONS: "block",
        config.ENV_RETENTION: "1h",
    })
    assert cfg.enabled is False


def test_cli_flag_beats_environment():
    args = SimpleNamespace(social_edges=True, social_db=None,
                           social_collections=None, social_retention=None)
    cfg = config.from_args(args, env={config.ENV_ENABLED: "0"})
    assert cfg.enabled is True and cfg.source == "cli"


def test_cli_absence_does_not_disable_environment():
    args = SimpleNamespace(social_edges=False, social_db=None,
                           social_collections=None, social_retention=None)
    cfg = config.from_args(args, env={config.ENV_ENABLED: "1"})
    assert cfg.enabled is True and cfg.source == "env"


def test_unknown_collection_is_refused():
    with pytest.raises(config.ConfigError) as e:
        config.from_env({config.ENV_ENABLED: "1",
                         config.ENV_COLLECTIONS: "block,posts"})
    assert "posts" in str(e.value)


def test_bad_retention_is_refused():
    with pytest.raises(config.ConfigError):
        config.from_env({config.ENV_ENABLED: "1",
                         config.ENV_RETENTION: "a fortnight"})


# --- receipts ---------------------------------------------------------------

def test_receipt_is_produced_in_both_states():
    off = config.from_env({}).public_receipt("run-1")
    on = config.from_env({config.ENV_ENABLED: "1"}).public_receipt("run-1")
    assert set(off) == set(on)
    assert off["enabled"] is False and on["enabled"] is True
    assert off["collections"] == [] and on["collections"]
    assert off["retention"] is None and on["retention"]


def test_public_receipt_carries_no_filesystem_path():
    cfg = config.from_env({config.ENV_ENABLED: "1",
                           config.ENV_DB: "/var/lib/secret/place.sqlite"})
    assert "db_path" not in cfg.public_receipt("r")
    assert cfg.receipt("r")["db_path"] == "/var/lib/secret/place.sqlite"
    assert "/var/lib" not in json.dumps(cfg.public_receipt("r"))


def test_config_hash_moves_with_scope_and_horizon():
    a = config.from_env({config.ENV_ENABLED: "1"})
    b = config.from_env({config.ENV_ENABLED: "1",
                         config.ENV_COLLECTIONS: "block"})
    c = config.from_env({config.ENV_ENABLED: "1",
                         config.ENV_RETENTION: "7d"})
    assert len({a.config_hash, b.config_hash, c.config_hash}) == 3


def test_collector_records_the_receipt_even_when_disabled(conn, tmp_path):
    from weatherwatch import db as weather_db
    from weatherwatch.cli import _record_social_receipt
    cfg = config.from_env({})
    receipt = _record_social_receipt(conn, cfg, "run-off", tmp_path)
    stored = json.loads(weather_db.get_meta(conn, config.RECEIPT_META_KEY))
    assert stored == receipt
    assert stored["enabled"] is False
    on_disk = json.loads((tmp_path / config.RECEIPT_FILENAME).read_text())
    assert on_disk["enabled"] is False and on_disk["db_path"] is None


# --- the sink actually respects the switch ---------------------------------

def test_disabled_means_no_social_writes_at_all(conn, tmp_path):
    """Not 'an empty database' — no database."""
    social = tmp_path / "social.sqlite"
    _drive(conn, None, _msgs())
    assert not social.exists()
    # The weather fixture's own database lives here too; what must be absent
    # is any social store, not any file.
    assert not list(tmp_path.glob("social*"))
    assert query.series(conn, [query.latest_run_id(conn)], "block.create").total == 30


def test_enabled_produces_the_expected_observations(conn, tmp_path):
    sink = SocialSink.open(tmp_path / "social.sqlite", run_id="run-on")
    _drive(conn, sink, _msgs())
    n = sink.conn.execute("SELECT COUNT(*) FROM edge_event").fetchone()[0]
    subjects = sink.conn.execute(
        "SELECT DISTINCT subject_ref FROM edge_event").fetchall()
    assert n == 30
    assert [r[0] for r in subjects] == ["did:plc:target"]
    sink.conn.close()


def _weather_rows(conn, run_id):
    return (
        [tuple(r) for r in conn.execute(
            "SELECT bucket_start, bucket_width, metric, count FROM bucket "
            "WHERE run_id=? ORDER BY bucket_start, metric", (run_id,))],
        [tuple(r) for r in conn.execute(
            "SELECT bucket_start, events_seen, parse_errors, unclassified, "
            "rejected_no_time_us, late_events, observed_duration_us, "
            "coverage_state, partial FROM window_health WHERE run_id=? "
            "ORDER BY bucket_start", (run_id,))],
    )


def test_weather_counters_are_identical_with_the_sink_on_and_off(tmp_path):
    """The switch must not perturb a single count in the weather lane."""
    from weatherwatch import db as weather_db

    msgs = _msgs()
    results = []
    for label, use_sink in (("off", False), ("on", True)):
        c = weather_db.connect(tmp_path / f"w-{label}.sqlite")
        weather_db.init_db(c)
        sink = (SocialSink.open(tmp_path / f"s-{label}.sqlite", run_id="r")
                if use_sink else None)
        col = _drive(c, sink, msgs)
        results.append(_weather_rows(c, col.run_id))
        if sink:
            sink.conn.close()
        c.close()

    off_buckets, off_health = results[0]
    on_buckets, on_health = results[1]
    assert off_buckets == on_buckets
    # window_health carries no wall-clock field, so the rows compare directly.
    assert off_health == on_health


# --- durability -------------------------------------------------------------

def test_buffer_flushes_on_age_not_only_on_size(edge_conn):
    """At ~5 edges/s a 2,000-row batch is ~7 minutes. The weather lane commits
    every 60s window; the two should not be an order of magnitude apart."""
    from .conftest import edge as mk
    w = store.EdgeWriter(edge_conn, "run-x", batch_rows=2_000,
                         flush_interval_s=60)
    w.add_edge(mk("did:plc:a", "did:plc:t", BASE_US))

    assert w.should_flush(BASE_US) is False          # arms the clock
    assert w.should_flush(BASE_US + 30 * 1_000_000) is False
    assert w.should_flush(BASE_US + 61 * 1_000_000) is True

    w.flush(BASE_US + 61 * 1_000_000)
    assert w.pending == 0
    assert w.should_flush(BASE_US + 200 * 1_000_000) is False, \
        "an empty buffer must never flush"


def test_size_trigger_still_wins_immediately(edge_conn):
    from .conftest import edge as mk
    w = store.EdgeWriter(edge_conn, "run-x", batch_rows=3, flush_interval_s=999)
    for i in range(3):
        w.add_edge(mk(f"did:plc:a{i}", "did:plc:t", BASE_US + i))
    assert w.should_flush(BASE_US) is True
