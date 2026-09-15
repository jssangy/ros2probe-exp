#!/usr/bin/env python3
"""Exchange per-trial schedules over the master's existing SSH channel."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.clock_sync import check_environment


def append_event(path, **event):
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(event) + '\n')


def request_time(directory, command, timeout=30):
    # The master checks both clocks before assigning a future epoch. Keep the
    # local check for direct supervisors that do not enable that barrier.
    clock = {} if os.environ.get('RP_EXP_MASTER_CLOCK_BARRIER') == '1' else check_environment()
    folder = Path(directory)
    token = uuid.uuid4().hex
    request = folder / (token + '.request.json')
    temporary = request.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(id=token, command=command, ready_ns=time.time_ns(), clock=clock)))
    temporary.replace(request)
    response = folder / (token + '.reply.json')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if response.exists():
            value = json.loads(response.read_text())
            target = value.get('target_ns')
            if value.get('id') != token or type(target) is not int or target <= time.time_ns():
                raise RuntimeError('Invalid or expired master schedule')
            return value
        time.sleep(.05)
    raise RuntimeError('Master did not provide a start time')


def wait_until(target_ns, log=None, command='', tolerance_ms=100, check_clock=True):
    if check_clock:
        check_environment()
    start_wall, start_mono = time.time_ns(), time.monotonic_ns()
    remaining = target_ns - start_wall
    if remaining <= 0 or remaining > 30_000_000_000:
        raise RuntimeError('Scheduled start was missed or is more than 30 seconds away')
    deadline = start_mono + remaining
    while True:
        delta = deadline - time.monotonic_ns()
        if delta <= 0:
            break
        time.sleep(min(delta / 1e9, .01))
    actual = time.time_ns()
    lateness = (actual - target_ns) / 1e6
    append_event(log, command=command, target_ns=target_ns, dispatch_ns=actual,
                 lateness_ms=lateness, event='dispatch')
    if abs(lateness) > tolerance_ms:
        raise RuntimeError(f'Scheduled dispatch missed tolerance: {lateness:.3f} ms')
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target_ns', type=int)
    parser.add_argument('--command', default='')
    parser.add_argument('--log')
    args = parser.parse_args()
    wait_until(args.target_ns, args.log, args.command,
               float(os.environ.get('RP_EXP_MAX_LATENESS_MS', '100')))


if __name__ == '__main__':
    main()
