"""Opt-in offline test: execute over real SSH with a real user systemd bus.

Usage: python3 tests/collector_readiness_real.py OUTPUT_DIRECTORY [CASE]
No production unit, source tree, database, or service configuration is used.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

spec = importlib.util.spec_from_file_location('readiness', Path(__file__).resolve().parents[1] / 'scripts/collector_readiness.py')
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)
output = Path(sys.argv[1]).resolve()
output.mkdir(parents=True, exist_ok=False)
cases = [sys.argv[2]] if len(sys.argv) > 2 else ['delay', 'deadline', 'expired', 'pid', 'invocation', 'invocation-only', 'source', 'argv', 'module', 'unavailable']

for case in cases:
    work = Path(tempfile.mkdtemp(prefix=case + '-', dir=output))
    release = work / 'release'
    release.mkdir()
    (release / 'weatherwatch.py').write_text('import time\ntime.sleep(20)\n')
    entry = release / 'entry.py'
    entry.write_text('import weatherwatch\n')
    launcher = work / 'launch.sh'
    launcher.write_text(f'#!/bin/sh\nsleep 0.8\nexec /usr/bin/python3 {entry}\n')
    launcher.chmod(0o700)
    unit = 'ww-readiness-' + uuid.uuid4().hex + '.service'
    config = {'unit': unit, 'user': True, 'argv': ['/usr/bin/python3', str(entry)],
              'cwd': str(release), 'module': str(release / 'weatherwatch.py'), 'timeout': 3, 'interval': 0.05}
    if case == 'deadline':
        config['timeout'] = 0.15
    if case == 'expired':
        config['timeout'] = 0
    if case == 'source':
        config['cwd'] = str(work)
    if case == 'module':
        config['module'] = str(work / 'wrong.py')
    if case == 'argv':
        config['argv'] = ['/usr/bin/python3', str(work / 'wrong-release.py')]
    subprocess.run(['systemd-run', '--user', '--quiet', '--unit', unit,
                    '--property=Type=simple', '--property=Restart=no',
                    '--working-directory', str(release), str(launcher)], check=True)
    receipt = {'observations': [], 'automatic_restart': False, 'automatic_restore': False}
    first = readiness.observe(config)
    receipt['original_single_sample'] = first
    receipt['original_release_in_cmdline'] = str(release) in ' '.join(first['argv'])
    assert not receipt['original_release_in_cmdline'], 'old predicate did not reproduce'
    live_config = dict(config)
    if case == 'unavailable':
        config['unit'] = 'ww-readiness-absent-' + uuid.uuid4().hex + '.service'
    # Restart only the fixture after its first observation: both the real
    # MainPID and InvocationID change while the observer is waiting for exec.
    original_observe = readiness.observe
    count = [0]
    def observe(config):
        state = original_observe(config)
        count[0] += 1
        if count[0] == 1 and case in ('pid', 'invocation'):
            subprocess.run(['systemctl', '--user', 'restart', unit], check=True)
        if count[0] > 1 and case == 'invocation-only':
            state['InvocationID'] = 'deterministic-invocation-only-control'
        return state
    readiness.observe = observe
    try:
        result = readiness.wait_ready(config, receipt)
        receipt['result'] = 'ready'
        assert case == 'delay', (case, 'unexpected readiness')
        assert result['argv'] == ['/usr/bin/python3', str(entry)]
        assert receipt['observations'][0]['argv'] != result['argv'], 'delay was not reproduced'
    except readiness.ReadinessError as exc:
        receipt['result'] = 'refused'
        receipt['error'] = str(exc)
        if case == 'delay':
            raise
    finally:
        readiness.observe = original_observe
        # Capture the live unit before explicit fixture teardown. The observer
        # must leave the producer alone even when readiness is refused.
        receipt['before_fixture_cleanup'] = original_observe(live_config)
        (work / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        subprocess.run(['systemctl', '--user', 'stop', unit], check=True)
        subprocess.run(['systemctl', '--user', 'reset-failed', unit], capture_output=True)
    print(case, receipt['result'], flush=True)
