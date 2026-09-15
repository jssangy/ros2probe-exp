#!/usr/bin/env python3
"""Configure the two slave laptops from the master; requires no ROS installation."""
import argparse
import json
from pathlib import Path
from testbed import ROOT, ROLES, validate


def ask(prompt, default=''):
    return input(prompt + (f' [{default}]' if default else '') + ': ').strip() or default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.testbed.json')
    args = parser.parse_args()
    previous = json.loads(args.output.read_text()) if args.output.exists() else {}
    distro = ask('ROS distro installed on BOTH slaves (humble/jazzy)', 'humble')
    if distro not in ('humble', 'jazzy'):
        parser.error('Use humble or jazzy')
    config = dict(master_clock_address=ask('This master IPv4 reachable by both slaves (NTP)', previous.get('master_clock_address', '')),
                  domain_id=int(ask('Dedicated ROS domain ID', str(previous.get('domain_id', 77)))),
                  link=ask('Active experiment link (wired/wireless); opposite link is disabled during runs', previous.get('link', 'wired')), hosts={})
    for role in ROLES:
        old = previous.get('hosts', {}).get(role, {})
        print(f'\n{role}:')
        config['hosts'][role] = dict(
            ssh=ask('SSH alias or user@IP (must reach the selected data IP)', old.get('ssh', '')),
            address=ask('Data-link IPv4 address', old.get('address', '')),
            nic=ask('Data NIC on this slave', old.get('nic', '')),
            repo=ask('Repository path on this slave', old.get('repo', '~/ros2probe-exp')),
            ros_setup=f'/opt/ros/{distro}/setup.bash',
            rp_bin=ask('rp path (blank uses bundled source build .tools/bin/rp)', old.get('rp_bin', '')))
    try:
        validate(config)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(config, indent=2) + '\n')
    print(f'Configuration: {args.output}')
    print('Next: python3 scripts/run_experiments.py build')


if __name__ == '__main__':
    main()
