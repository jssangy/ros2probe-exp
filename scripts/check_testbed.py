#!/usr/bin/env python3
"""Slave-side checks for the configured data link and experiment dependencies."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

from install_rp import ROOT, installed_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['slave_a', 'slave_b'], required=True)
    parser.add_argument('--experiment', choices=['all', '1', '2', '3', '4'], default='all')
    parser.add_argument('--address', required=True)
    parser.add_argument('--peer', required=True)
    parser.add_argument('--nic', required=True)
    args = parser.parse_args()
    addresses = json.loads(subprocess.check_output(['ip', '-j', 'address', 'show', 'dev', args.nic]))
    if args.address not in [entry.get('local') for row in addresses for entry in row.get('addr_info', [])]:
        raise RuntimeError('Configured data IP is not assigned to the configured NIC')
    route = json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', args.peer]))[0]
    if route.get('dev') != args.nic or route.get('prefsrc') != args.address:
        raise RuntimeError('Route to the other slave does not use the configured NIC/source IP')
    for command in ('rsync', 'chronyc'):
        if not shutil.which(command):
            raise RuntimeError(f'Missing command: {command}')
    selected = ['1', '2', '3', '4'] if args.experiment == 'all' else [args.experiment]
    for exp in selected:
        role = 'receiver' if exp == '1' or args.role == 'slave_b' else 'publisher'
        subprocess.run(['python3', 'scripts/check_environment.py', exp, '--role', role,
                        '--nic', args.nic], check=True)
    rp = os.environ.get('RP_BIN') or str(ROOT / '.tools/bin/rp')
    build = installed_manifest() if Path(rp).resolve() == (ROOT / '.tools/bin/rp').resolve() else None
    metadata = dict(hostname=platform.node(), kernel=platform.release(),
                    ros_distro=os.environ.get('ROS_DISTRO'), domain=os.environ.get('ROS_DOMAIN_ID'),
                    role=args.role, address=args.address, peer=args.peer, nic=args.nic,
                    rp_binary=rp, rp_build=build,
                    rp_sha256=hashlib.sha256(Path(rp).read_bytes()).hexdigest() if rp and Path(rp).is_file() else None)
    (Path(os.environ['RP_EXP_RESULTS_ROOT']) / '_master' / 'environment.json').write_text(
        json.dumps(metadata, indent=2) + '\n')
    print('[PASS] Slave environment and data route')


if __name__ == '__main__':
    main()
