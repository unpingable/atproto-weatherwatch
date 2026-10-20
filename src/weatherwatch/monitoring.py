"""Application-owned bounded progress snapshot and ordinary Prometheus output.

The observer reads files only. SQL runs solely on the collector's existing
connection, for its exact current run and indexed latest committed window.
Legacy visibility remains available for integrity and publication-gate parity.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import tempfile

from . import db, timeutil, visibility as legacy

SCHEMA = "weatherwatch.monitoring/v1"
LIMIT = 65536
MAX_AGE = 60


def atomic(path: Path, data: bytes) -> None:
    if len(data) > LIMIT:
        raise ValueError("monitoring output exceeds 64 KiB")
    fd, temporary = tempfile.mkstemp(prefix=".weatherwatch-monitoring-", dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    with path.open('rb') as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("monitoring input exceeds 64 KiB")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("monitoring input must be an object")
    return result


def clock(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except ValueError:
        return None


def recent(value, now, limit):
    stamp = clock(value)
    return stamp is not None and 0 <= now.timestamp() - stamp <= limit


def publish_snapshot(conn, run_id: str, runtime: dict, path: Path, metrics_path: Path | None = None) -> None:
    """Call only after this application's own successful committed runtime write."""
    if conn.in_transaction:
        raise ValueError("monitoring requires committed state")
    run = conn.execute('SELECT * FROM observation_run WHERE run_id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError("monitoring run absent")
    run = dict(run)
    row = conn.execute('SELECT * FROM window_health WHERE run_id=? ORDER BY bucket_start DESC, bucket_width DESC LIMIT 1',
                       (run_id,)).fetchone()
    cursor = db.get_cursor(conn, run['source_endpoint'])
    schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    # Inspect only fixed schema tables, never application data or whole-store integrity.
    required = ('meta', 'observation_run', 'bucket', 'window_health')
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('meta','observation_run','bucket','window_health','event','events','raw_event','raw_events')")}
    forbidden = {'did', 'handle', 'rkey', 'cid', 'uri', 'text', 'actor', 'subject'}
    identity_columns = [table + '.' + row['name'] for table in required if table in names
                        for row in conn.execute('PRAGMA table_info(' + table + ')') if row['name'].lower() in forbidden]
    snapshot = {'missing_tables': sorted(set(required) - names), 'schema_version': schema[0] if schema else None,
                'runtime': runtime, 'cursors': {run['source_endpoint']: cursor} if cursor is not None else {},
                'cursor_errors': [], 'latest_run': run, 'latest_health': dict(row) if row else None,
                'identity_columns': identity_columns, 'raw_event_tables': sorted(names & {'event', 'events', 'raw_event', 'raw_events'})}
    document = {'schema': SCHEMA, 'observed_at': runtime['updated_at'], 'run_id': run_id,
                'write_commit_observed': True, 'snapshot': snapshot}
    atomic(Path(path), (json.dumps(document, sort_keys=True, allow_nan=False) + '\n').encode())
    if metrics_path is not None:
        atomic(Path(metrics_path), progress_metrics(document).encode())


def concern(identity, state, reason, observed_at=None):
    return {'id': identity, 'required': True, 'observation': {
        'local_state': state, 'reason': reason, 'observed_at': observed_at, 'valid_for_seconds': MAX_AGE}}


def report_concerns(directory: Path, now):
    """Read bounded metadata and current content gate; metadata alone cannot establish eligibility."""
    ids = ('weatherwatch.report.candidate', 'weatherwatch.publication.gate')
    try:
        summary = read_json(directory / 'summary.json')
        generated = summary.get('generated_at')
        freshness = summary.get('freshness') or {}
        newest = clock(freshness.get('newest_observation_end'))
        generation = clock(generated)
        limit = freshness.get('current_threshold_seconds')
        artifact_limit = 2 * legacy.PUBLISH_INTERVAL_S + legacy.PUBLISH_RENDER_ALLOWANCE_S
        if not recent(generated, now, artifact_limit):
            state, reason = 'STALE', 'candidate generation absent, stale or future'
        elif (newest is None or generation is None or not isinstance(limit, (int, float))
              or isinstance(limit, bool) or not math.isfinite(limit) or limit <= 0
              or not 0 <= generation - newest <= limit):
            state, reason = 'UNKNOWN', 'candidate source time is missing, stale or future'
        elif freshness.get('state') == 'current':
            state, reason = 'PRESENT', 'candidate metadata records a current source at generation'
        elif freshness.get('state') == 'partial':
            state, reason = 'DEGRADED', 'candidate source observation is partial'
        else:
            state, reason = 'UNKNOWN', 'candidate source freshness is unavailable'
        candidate = concern(ids[0], state, reason, now.isoformat())
        candidate['observation']['facts'] = {'candidate_generated_at': generated, 'source_observation_end': freshness.get('newest_observation_end')}
        gate = legacy.evaluate_candidate(directory, max_files=256, max_bytes=16 * 1024 * 1024)
        if gate.get('missing_artifacts'):
            candidate = concern(ids[0], 'DEGRADED', 'candidate required artifacts are missing', now.isoformat())
        gate_state = {'PASSED': 'PRESENT', 'REFUSED': 'REFUSED', 'ERROR': 'UNKNOWN', 'NOT_EVALUATED': 'UNKNOWN'}[gate['disposition']]
        if candidate['observation']['local_state'] != 'PRESENT' and gate_state == 'PRESENT':
            gate_state = 'UNKNOWN'
        return [candidate, concern(ids[1], gate_state,
                                  'bounded current candidate gate: ' + gate.get('reason', 'unknown'), now.isoformat())]
    except (OSError, ValueError, TypeError, KeyError):
        return [locals().get('candidate', concern(ids[0], 'UNKNOWN', 'candidate metadata unavailable')),
                concern(ids[1], 'UNKNOWN', 'current bounded candidate gate unavailable')]


def snapshot_facts(document, now):
    """One validation path for status reads and writer/manual metrics output."""
    if document.get('schema') != SCHEMA or not recent(document.get('observed_at'), now, MAX_AGE):
        raise ValueError('snapshot missing, stale or future')
    snapshot = document['snapshot']
    run, health, runtime = snapshot['latest_run'], snapshot.get('latest_health'), snapshot['runtime']
    if (document.get('run_id') != run['run_id'] or runtime.get('run_id') != run['run_id']
            or runtime.get('endpoint') != run['source_endpoint'] or document.get('write_commit_observed') is not True
            or not recent(runtime.get('updated_at'), now, MAX_AGE)):
        raise ValueError('snapshot runtime binding, commit or clock invalid')
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not number(run['bucket_width']) or run['bucket_width'] <= 0:
        raise ValueError('invalid run width')
    if health is not None and (not number(health['bucket_start']) or not number(health['bucket_width'])
                               or health['bucket_width'] <= 0 or health['bucket_width'] != run['bucket_width']
                               or health.get('run_id') != run['run_id']
                               or not 0 <= health['bucket_start'] + health['bucket_width'] <= now.timestamp()):
        raise ValueError('invalid or future committed window')
    return snapshot, run, health, runtime


def progress_metrics(document, now=None):
    """Render current producer facts; observation time is never scrape time."""
    now = now or timeutil.now_utc()
    try:
        snapshot, run, health, runtime = snapshot_facts(document, now)
        rows = [('service_observation_timestamp_seconds', clock(document['observed_at']))]
        if health and run['ended_at'] is None:
            end = health['bucket_start'] + health['bucket_width']
            if not math.isfinite(end) or not 0 <= end <= now.timestamp() or health['bucket_width'] <= 0:
                return ''
            rows.append(('service_last_success_timestamp_seconds', end))
            lag = health.get('lag_ewma_s')
            current = now.timestamp() - end <= 2 * run['bucket_width'] + legacy.LATE_EVENT_GRACE_S
            if current and health.get('events_seen', 0) > 0 and isinstance(lag, (int, float)) and math.isfinite(lag) and lag >= 0:
                rows.append(('service_cursor_lag_seconds', lag))
        return ''.join('# TYPE %s gauge\n%s{service="weatherwatch"} %s\n' % (name, name, value) for name, value in rows)
    except (ValueError, TypeError, KeyError, AttributeError):
        return ''


def observe(snapshot_path, report_dir, now=None, social_db=None):
    now = now or timeutil.now_utc()
    metrics = ''
    ids = [identity for identity, _, _ in legacy.CONCERNS if identity != 'weatherwatch.persistence.social-store']
    try:
        document = read_json(Path(snapshot_path))
        snapshot, run, health, runtime = snapshot_facts(document, now)
        access = {'exists': True, 'readable': True, 'write_transaction_available': True}
        concerns = legacy._database_concerns(access, snapshot, now)
        for item in concerns:
            if item['concern_id'] == 'weatherwatch.persistence.access':
                item['summary'] = 'application recorded its own successful commit recently; whole-store integrity is not checked'
        rows = []
        for item in concerns:
            row = concern(item['concern_id'], item['state'], item['summary'], document['observed_at'])
            row['observation']['facts'] = {'source_observed_at': item.get('observed_at'), 'snapshot_observed_at': document['observed_at']}
            rows.append(row)
        if run['ended_at'] is not None:
            for row in rows:
                if row['id'] not in ('weatherwatch.persistence.access', 'weatherwatch.persistence.continuity'):
                    row['observation'].update(local_state='UNKNOWN', reason='run ended; historical window is not present activity')
        metrics = progress_metrics(document, now)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        rows = [concern(identity, 'UNKNOWN', 'committed progress snapshot unavailable, stale, malformed or future') for identity in ids if identity not in ('weatherwatch.report.candidate', 'weatherwatch.publication.gate')]
    social = legacy._social_store_concern(Path(social_db) if social_db else Path(snapshot_path).with_name('social.sqlite'), now)
    rows.append(concern(social['concern_id'], social['state'], social['summary'], social.get('observed_at')))
    rows.extend(report_concerns(Path(report_dir), now))
    output = {'schema': 'project.ops.status/v1', 'project': 'weatherwatch', 'generated_at': now.isoformat(), 'concerns': rows}
    return output, metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--report-dir', required=True)
    parser.add_argument('--social-db', help='default social.sqlite beside snapshot; header and WAL sizes only')
    parser.add_argument('--format', choices=('status', 'prometheus'), default='status')
    parser.add_argument('--prometheus-output', '--metrics-output', dest='metrics_output', help='atomically publish Prometheus textfile as this application account')
    args = parser.parse_args(argv)
    status, metrics = observe(args.snapshot, args.report_dir, social_db=args.social_db)
    if args.metrics_output:
        atomic(Path(args.metrics_output), metrics.encode())
    print(json.dumps(status, sort_keys=True) if args.format == 'status' else metrics, end='\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
