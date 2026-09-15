#!/usr/bin/env python3
"""Synchronize before measurement; only inspect clocks while a trial is armed."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time


def chronyc(*args, privileged=False):
    argv = (['sudo', '-n'] if privileged else []) + ['chronyc', *map(str, args)]
    return subprocess.check_output(argv, text=True, timeout=10).strip()


def tracking():
    fields = chronyc('-c', '-n', 'tracking').split(',')
    if len(fields) != 14 or fields[-1] != 'Normal':
        raise RuntimeError('chrony is not synchronized: ' + ','.join(fields))
    return fields


def check(source='', tolerance_ms=1):
    chronyc('waitsync', 1, tolerance_ms / 1000)
    values = tracking()
    if source and values[0].upper() != f'{int(ipaddress.IPv4Address(source)):08X}':
        raise RuntimeError(f'Clock must select master {source}; actual reference: {values[0]}')
    if abs(float(values[4])) > tolerance_ms / 1000:
        raise RuntimeError('Clock correction changed beyond tolerance during readiness inspection')
    return dict(reference_id=values[0], system_offset_s=float(values[4]),
                root_delay_s=float(values[10]), root_dispersion_s=float(values[11]),
                tracking_csv=','.join(values), checked_ns=time.time_ns())


def check_environment():
    return check(os.environ.get('RP_EXP_CLOCK_SOURCE', ''),
                 float(os.environ.get('RP_EXP_CLOCK_TOLERANCE_MS', '1')))


def wait_ready(source='', tolerance_ms=1, timeout=15):
    """Wait for ordinary slewing; never change chronyd or step an armed clock."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check(source, tolerance_ms)
        except (RuntimeError, subprocess.CalledProcessError) as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Clock did not settle within {timeout}s: {error}') from error
        time.sleep(.25)


def burst_in_progress():
    counts = [int(value) for value in chronyc('-c', 'activity').split(',')]
    if len(counts) != 5 or any(value < 0 for value in counts):
        raise RuntimeError('Invalid chrony activity report')
    return counts[2] + counts[3] > 0


def synchronize(source='', clients=(), tolerance_ms=1, timeout=90, reset_sources=False):
    if reset_sources and not source:
        raise ValueError('Reset source measurements only on a slave with a required master')
    if clients:
        current_reference = chronyc('-c', '-n', 'tracking').split(',')[0].upper()
        if current_reference in {f'{int(ipaddress.IPv4Address(client)):08X}' for client in clients}:
            raise RuntimeError('Master must not synchronize from either slave (clock source loop)')
    if source:
        source = str(ipaddress.IPv4Address(source))
        sources = chronyc('-c', '-n', 'sources')
        if not any(len(row.split(',')) > 2 and row.split(',')[2] == source
                   for row in sources.splitlines()):
            chronyc('add', 'server', source, 'iburst', 'prefer', privileged=True)
    for client in clients:
        chronyc('allow', str(ipaddress.IPv4Address(client)) + '/32', privileged=True)
    if reset_sources:
        # Changing Ethernet/Wi-Fi invalidates the old path's delay distribution.
        # Retain chrony source options and filters, but learn from the current link.
        print('[clock] Reset previous source measurements after link isolation', flush=True)
        chronyc('reset', 'sources', privileged=True)
        chronyc('online', source, privileged=True)
    # Demand a fresh sample before stepping; do not leave a future makestep armed.
    before = chronyc('-c', '-n', 'tracking').split(',')[3]
    if source:
        chronyc('burst', '4/8', source, privileged=True)
    else:
        chronyc('burst', '4/4', privileged=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            values = tracking()
            correct_source = not source or values[0].upper() == f'{int(ipaddress.IPv4Address(source)):08X}'
            # A first good sample can precede further burst corrections. Do not
            # release the workers until the entire requested burst has finished.
            if values[3] != before and correct_source and not burst_in_progress():
                chronyc('makestep', privileged=True)
                result = check(source, tolerance_ms)
                result['source_measurements_reset'] = reset_sources
                return result
        except (RuntimeError, subprocess.CalledProcessError):
            pass
        time.sleep(0.5)
    raise RuntimeError('No fresh synchronized clock sample from the required source; refusing experiment')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='')
    parser.add_argument('--allow', nargs='*', default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-only', action='store_true')
    mode.add_argument('--reset-sources', action='store_true',
                      help='discard previous slave clock samples after changing the data link')
    parser.add_argument('--tolerance-ms', type=float, default=1)
    parser.add_argument('--wait-seconds', type=float, default=0,
                        help='bounded read-only readiness wait; requires --check-only')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.wait_seconds < 0 or args.wait_seconds > 60 or (args.wait_seconds and not args.check_only):
        parser.error('--wait-seconds must be between 0 and 60 and requires --check-only')
    result = (wait_ready(args.source, args.tolerance_ms, args.wait_seconds) if args.check_only else
              synchronize(args.source, args.allow, args.tolerance_ms, reset_sources=args.reset_sources))
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
