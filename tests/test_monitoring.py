"""Bounded application-owned monitoring, including missing/frozen/future inputs."""
import datetime as dt
import json
from pathlib import Path
import sqlite3

import pytest
from weatherwatch import db, monitoring, visibility
from tests.test_visibility import _active, _runtime, _candidate, NOW, SYNTH_BASE


def publish(conn, tmp_path, *, empty=False):
    _active(conn, {'metrics': {}, 'events_seen': 0 if empty else 5, 'observed_us': 60_000_000},
            cursor=(SYNTH_BASE + 59) * 1_000_000)
    runtime = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (visibility.RUNTIME_META_KEY,)).fetchone()[0])
    path = tmp_path / 'monitoring.json'
    monitoring.publish_snapshot(conn, 'r1', runtime, path)
    return path


def states(status):
    return {row['id']: row['observation']['local_state'] for row in status['concerns']}


def test_committed_window_progress_and_observation_clock_are_separate(conn, tmp_path):
    path = publish(conn, tmp_path)
    status, metrics = monitoring.observe(path, tmp_path / 'report', NOW)
    assert 'service_observation_timestamp_seconds{service="weatherwatch"} ' + str(NOW.timestamp()) in metrics
    assert 'service_last_success_timestamp_seconds{service="weatherwatch"} ' + str(SYNTH_BASE + 60) in metrics
    assert 'service_cursor_lag_seconds' in metrics
    assert states(status)['weatherwatch.aggregate.production'] == 'PRESENT'
    assert states(status)['weatherwatch.persistence.access'] == 'PRESENT'
    assert len(status['concerns']) == 11


def test_empty_observed_window_is_useful_product_but_has_no_event_lag(conn, tmp_path):
    path = publish(conn, tmp_path, empty=True)
    status, metrics = monitoring.observe(path, tmp_path / 'report', NOW)
    assert states(status)['weatherwatch.acquisition.cursor'] == 'PRESENT'
    assert states(status)['weatherwatch.aggregate.production'] == 'PRESENT'
    assert 'service_last_success_timestamp_seconds' in metrics
    assert 'service_cursor_lag_seconds' not in metrics


def test_missing_stale_future_and_frozen_snapshots_do_not_clear(conn, tmp_path):
    path = publish(conn, tmp_path)
    original = json.loads(path.read_text())
    for offset in (-61, 1):
        document = dict(original, observed_at=(NOW + dt.timedelta(seconds=offset)).isoformat())
        path.write_text(json.dumps(document))
        status, metrics = monitoring.observe(path, tmp_path / 'report', NOW)
        assert metrics == ''
        assert states(status)['weatherwatch.observation.coverage'] == 'UNKNOWN'
    path.unlink()
    status, metrics = monitoring.observe(path, tmp_path / 'report', NOW)
    assert metrics == ''
    assert states(status)['weatherwatch.persistence.access'] == 'UNKNOWN'


def test_future_window_and_mismatched_run_are_unknown(conn, tmp_path):
    path = publish(conn, tmp_path)
    original = json.loads(path.read_text())
    for mutate in (lambda d: d.update(run_id='other'),
                   lambda d: d['snapshot']['latest_health'].update(bucket_start=NOW.timestamp() + 60)):
        doc = json.loads(json.dumps(original))
        mutate(doc)
        path.write_text(json.dumps(doc))
        status, metrics = monitoring.observe(path, tmp_path / 'report', NOW)
        assert metrics == ''
        assert states(status)['weatherwatch.acquisition.cursor'] == 'UNKNOWN'


def test_observer_never_opens_sqlite_and_uses_bounded_candidate_gate(conn, tmp_path, monkeypatch):
    path = publish(conn, tmp_path)
    report = tmp_path / 'report'
    _candidate(report, generated=NOW, newest=NOW - dt.timedelta(seconds=10))
    def forbidden(*a, **kw):
        raise AssertionError('unbounded observer operation')
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    monkeypatch.setattr(Path, 'rglob', forbidden)
    status, _ = monitoring.observe(path, report, NOW)
    assert states(status)['weatherwatch.report.candidate'] == 'PRESENT'
    assert states(status)['weatherwatch.publication.gate'] == 'PRESENT'
    assert sorted(p.name for p in tmp_path.glob('*')) == ['monitoring.json', 'report', 'test.sqlite', 'test.sqlite-shm', 'test.sqlite-wal']


def test_unchanged_summary_cannot_clear_changed_candidate_content(conn, tmp_path):
    path = publish(conn, tmp_path)
    report = tmp_path / 'report'
    _candidate(report, generated=NOW, newest=NOW - dt.timedelta(seconds=10))
    assert states(monitoring.observe(path, report, NOW)[0])['weatherwatch.publication.gate'] == 'PRESENT'
    (report / 'index.html').write_text('did:plc:abcdefghijklmnopqrstuvwx')
    assert states(monitoring.observe(path, report, NOW)[0])['weatherwatch.publication.gate'] == 'REFUSED'
    (report / 'index.html').unlink()
    output = states(monitoring.observe(path, report, NOW)[0])
    assert output['weatherwatch.report.candidate'] == 'DEGRADED'
    assert output['weatherwatch.publication.gate'] == 'REFUSED'


def test_candidate_gate_budget_exhaustion_cannot_clear(conn, tmp_path):
    path = publish(conn, tmp_path)
    report = tmp_path / 'report'
    _candidate(report, generated=NOW, newest=NOW - dt.timedelta(seconds=10))
    (report / 'huge.bin').write_bytes(b'x' * (16 * 1024 * 1024 + 1))
    assert states(monitoring.observe(path, report, NOW)[0])['weatherwatch.publication.gate'] == 'UNKNOWN'
    (report / 'huge.bin').unlink()
    for index in range(257):
        (report / str(index)).touch()
    assert states(monitoring.observe(path, report, NOW)[0])['weatherwatch.publication.gate'] == 'UNKNOWN'


def test_snapshot_queries_use_current_run_and_indexed_window_no_integrity_scan(conn, tmp_path):
    statements = []
    conn.set_trace_callback(statements.append)
    publish(conn, tmp_path)
    conn.set_trace_callback(None)
    queries = statements[statements.index(next(s for s in statements if s.startswith('SELECT * FROM observation_run WHERE'))):]
    assert not any('quick_check' in s or 'BEGIN' in s or 'SELECT * FROM bucket' in s for s in queries)
    assert any('WHERE run_id=' in s and 'LIMIT 1' in s for s in queries)
    plans = list(conn.execute("EXPLAIN QUERY PLAN SELECT * FROM window_health WHERE run_id=? ORDER BY bucket_start DESC, bucket_width DESC LIMIT 1", ('r1',)))
    assert any('PRIMARY KEY' in row['detail'] for row in plans)


def test_output_bound_and_interrupted_publication_leave_old_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'monitoring.json'
    path.write_text('old')
    with pytest.raises(ValueError):
        monitoring.atomic(path, b'x' * (monitoring.LIMIT + 1))
    def fail(*a):
        raise OSError('test publication interruption')
    monkeypatch.setattr(monitoring.os, 'replace', fail)
    with pytest.raises(OSError):
        monitoring.atomic(path, b'new')
    assert path.read_text() == 'old'
    assert list(tmp_path.iterdir()) == [path]


def test_collector_snapshot_opt_in_uses_committed_writer_and_failure_is_optional(conn, tmp_path, monkeypatch):
    from weatherwatch.collector import Collector
    collector = Collector(conn, 'wss://fixture.invalid', monitoring_snapshot=tmp_path / 'snapshot.json')
    collector._start_run()
    assert json.loads((tmp_path / 'snapshot.json').read_text())['run_id'] == collector.run_id
    monkeypatch.setattr(monitoring, 'publish_snapshot', lambda *a: (_ for _ in ()).throw(OSError('fixture')))
    collector._record_runtime('connected')  # diagnostics cannot abort collection


def test_heartbeat_does_not_refresh_useful_progress_and_stopped_run_is_not_success(conn, tmp_path):
    path = publish(conn, tmp_path)
    document = json.loads(path.read_text())
    later = NOW + dt.timedelta(seconds=30)
    document['observed_at'] = later.isoformat()
    document['snapshot']['runtime']['updated_at'] = later.isoformat()
    text = monitoring.progress_metrics(document, later)
    assert 'service_observation_timestamp_seconds{service="weatherwatch"} ' + str(later.timestamp()) in text
    assert 'service_last_success_timestamp_seconds{service="weatherwatch"} ' + str(SYNTH_BASE + 60) in text
    document['snapshot']['latest_run']['ended_at'] = later.isoformat()
    assert 'service_last_success_timestamp_seconds' not in monitoring.progress_metrics(document, later)


def test_prometheus_output_and_manifest_match_authored_service(conn, tmp_path, monkeypatch, capsys):
    import tomllib
    path = publish(conn, tmp_path)
    monkeypatch.setattr(monitoring.timeutil, 'now_utc', lambda: NOW)
    output = tmp_path / 'weatherwatch.prom'
    assert monitoring.main(['--snapshot', str(path), '--report-dir', str(tmp_path / 'report'),
                            '--format', 'prometheus', '--prometheus-output', str(output)]) == 0
    assert output.read_text() in capsys.readouterr().out
    assert '{service="weatherwatch"}' in output.read_text()
    manifest = tomllib.loads(Path('.ops/monitoring-concerns.toml').read_text())
    assert [row['id'] for row in manifest['concerns']] == [row[0] for row in visibility.CONCERNS]
    assert Path('src/weatherwatch/_ops/monitoring-concerns.toml').read_bytes() == Path('.ops/monitoring-concerns.toml').read_bytes()
    declaration = tomllib.loads(Path('.ops/service.toml').read_text())
    assert declaration['status']['user'] == 'weatherwatch'
    assert declaration['thresholds']['last_success_age_seconds'] == 125
    assert 'cursor_lag_seconds' not in declaration['thresholds']


def test_consumer_freshness_uses_assessment_source_not_report_or_window_epoch(conn, tmp_path):
    path = publish(conn, tmp_path)
    later = NOW + dt.timedelta(seconds=70)
    document = json.loads(path.read_text())
    document['observed_at'] = later.isoformat()
    document['snapshot']['runtime']['updated_at'] = later.isoformat()
    path.write_text(json.dumps(document))
    report = tmp_path / 'report'
    _candidate(report, generated=later - dt.timedelta(seconds=900), newest=later - dt.timedelta(seconds=910))
    status, _ = monitoring.observe(path, report, later)
    indexed = {row['id']: row['observation'] for row in status['concerns']}
    assert indexed['weatherwatch.aggregate.production']['local_state'] == 'PRESENT'
    assert indexed['weatherwatch.report.candidate']['local_state'] == 'PRESENT'
    # The actual estate consumer ages every PRESENT observation using its own
    # observed_at + valid_for_seconds, independent of the producer's document age.
    for row in indexed.values():
        if row['local_state'] == 'PRESENT':
            age = later.timestamp() - monitoring.clock(row['observed_at'])
            assert 0 <= age <= row['valid_for_seconds']
    assert indexed['weatherwatch.aggregate.production']['facts']['source_observed_at'] != document['observed_at']
    assert indexed['weatherwatch.report.candidate']['facts']['candidate_generated_at'] != later.isoformat()
    stale_status, _ = monitoring.observe(path, report, later + dt.timedelta(seconds=61))
    assert states(stale_status)['weatherwatch.aggregate.production'] == 'UNKNOWN'


def test_status_and_metrics_share_runtime_and_finite_width_validation(conn, tmp_path):
    path = publish(conn, tmp_path)
    original = json.loads(path.read_text())
    for mutate in (lambda d: d['snapshot']['runtime'].update(updated_at=(NOW - dt.timedelta(seconds=61)).isoformat()),
                   lambda d: d['snapshot']['runtime'].update(updated_at=(NOW + dt.timedelta(seconds=1)).isoformat()),
                   lambda d: d['snapshot']['latest_health'].update(bucket_width=True),
                   lambda d: d['snapshot']['latest_run'].update(bucket_width=True)):
        document = json.loads(json.dumps(original))
        mutate(document)
        path.write_text(json.dumps(document))
        assert monitoring.progress_metrics(document, NOW) == ''
        status, metrics = monitoring.observe(path, tmp_path / 'report', NOW)
        assert metrics == ''
        assert states(status)['weatherwatch.observation.coverage'] == 'UNKNOWN'
