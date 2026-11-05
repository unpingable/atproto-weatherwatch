#!/usr/bin/env python3
"""Read-only collector startup observation for guarded release selectors."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


class ReadinessError(RuntimeError):
    pass


def observe(config):
    command = ['systemctl'] + (['--user'] if config.get('user') else [])
    result = subprocess.run(command + ['show', config['unit'], '-p', 'MainPID',
        '-p', 'InvocationID', '-p', 'ActiveState', '-p', 'SubState'],
        capture_output=True, text=True, check=True, timeout=2)
    state = dict(line.split('=', 1) for line in result.stdout.splitlines())
    pid = int(state['MainPID'])
    proc = Path('/proc') / str(pid)
    state['argv'] = proc.joinpath('cmdline').read_bytes().rstrip(b'\0').decode().split('\0')
    state['starttime'] = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()[19]
    state['cwd'] = str(proc.joinpath('cwd').resolve(strict=True))
    state['exe'] = str(proc.joinpath('exe').resolve(strict=True))
    return state


def wait_ready(config, receipt):
    timeout = float(config['timeout'])
    interval = float(config.get('interval', 0.1))
    if not 0 < timeout <= 30 or not 0 < interval <= 1:
        raise ReadinessError('expired or invalid readiness deadline')
    deadline = time.monotonic() + timeout
    identity = None
    while True:
        if time.monotonic() >= deadline:
            raise ReadinessError('readiness deadline expired')
        try:
            sample = observe(config)
            receipt['observations'].append(sample)
            current = (sample['MainPID'], sample['InvocationID'], sample['starttime'])
            if int(current[0]) <= 0 or not current[1]:
                raise ReadinessError('readiness identity unavailable')
            if identity is None:
                identity = current
            if current != identity:
                raise ReadinessError('collector PID or invocation changed')
            if sample['ActiveState'] != 'active' or sample['SubState'] != 'running':
                raise ReadinessError('collector is not running')
            if sample['argv'] == config['argv'] and sample['cwd'] == config['cwd']:
                if sample['exe'] != str(Path(config['argv'][0]).resolve(strict=True)):
                    raise ReadinessError('selected interpreter differs')
                proc = Path('/proc') / sample['MainPID']
                environment = dict(entry.decode().split('=', 1) for entry in
                    proc.joinpath('environ').read_bytes().split(b'\0') if entry)
                # Resolve the module in the observed process's environment and
                # directory. Never store environment bytes in the receipt.
                probe = subprocess.run([config['argv'][0], '-c',
                    'import importlib.util,json; s=importlib.util.find_spec("weatherwatch"); '
                    'print(json.dumps(s.origin if s else None))'],
                    cwd=sample['cwd'], env=environment, capture_output=True,
                    text=True, check=True, timeout=max(0.001, deadline-time.monotonic()))
                module = json.loads(probe.stdout)
                if module != config['module']:
                    raise ReadinessError('selected module source differs')
                after = observe(config)
                receipt['observations'].append(after)
                if after != sample:
                    raise ReadinessError('collector changed during readiness observation')
                if time.monotonic() >= deadline:
                    raise ReadinessError('readiness deadline expired')
                receipt['module_resolution'] = module
                return after
        except ReadinessError:
            raise
        except Exception as exc:
            raise ReadinessError('readiness observation unavailable: ' + type(exc).__name__) from exc
        time.sleep(min(interval, max(0, deadline-time.monotonic())))


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    receipt = {'observations': [], 'automatic_restart': False, 'automatic_restore': False}
    try:
        receipt['ready'] = wait_ready(config, receipt)
    except Exception as exc:
        receipt['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        Path(sys.argv[2]).write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
