#!/usr/bin/env python3
"""Dispatch six self-contained master launchers to the shared implementation."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

from testbed import ROOT, default_config

EXPERIMENTS = {'exp1': ('1', 'wired'), 'exp2': ('2', 'wired'), 'exp3': ('3', 'wired'),
               'exp4-wired': ('4', 'wired'), 'exp4-wireless': ('4', 'wireless')}


def command(mode, argv):
    if mode == 'dependencies':
        if any(arg == '--link' or arg.startswith('--link=') for arg in argv):
            raise ValueError('install_dependencies.sh always uses wired SSH; do not pass --link')
        return [sys.executable, str(ROOT / 'scripts/setup_testbed.py'),
                '--link', 'wired', '--build-workloads', *argv]
    if mode not in EXPERIMENTS:
        raise ValueError('Unknown launcher: ' + mode)
    experiment, link = EXPERIMENTS[mode]
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--config', type=Path, default=default_config())
    parser.add_argument('--run-id')
    args, forwarded = parser.parse_known_args(argv)
    run_id = args.run_id or mode + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return [sys.executable, str(ROOT / 'scripts/run_experiments.py'), '--config', str(args.config),
            '--link', link, 'run', experiment, '--run-id', run_id, '--prepare-ssh', *forwarded]


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Use a launcher in the repository root')
    try:
        argv = command(sys.argv[1], sys.argv[2:])
    except ValueError as error:
        raise SystemExit(str(error)) from error
    # Replace the dispatcher: the existing runner owns Ctrl-C and remote cleanup.
    os.execv(argv[0], argv)


if __name__ == '__main__':
    main()
