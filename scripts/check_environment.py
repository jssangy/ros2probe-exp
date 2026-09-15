#!/usr/bin/env python3
"""Read-only dependency checks. Does not start DDS, eBPF, chrony, or packet capture."""
import argparse
import importlib
import os
from pathlib import Path
import shutil
import subprocess

from install_rp import ROOT, installed_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', nargs='?', default='all', choices=['all', '1', '2', '3', '4'])
    parser.add_argument('--role', choices=['publisher', 'receiver'], default='receiver')
    parser.add_argument('--nic')
    parser.add_argument('--skip-sudo', action='store_true', help='omit privilege check for local code validation')
    args = parser.parse_args()
    selected = {'1', '2', '3', '4'} if args.experiment == 'all' else {args.experiment}
    failures = []

    def check(label, ok, detail=''):
        print(f'[{"PASS" if ok else "FAIL"}] {label}' + (f': {detail}' if detail else ''))
        if not ok:
            failures.append(label)

    commands = {'ros2', 'python3', 'setsid', 'timeout', 'ip', 'sudo'}
    if selected & {'2', '3', '4'}:
        commands.add('nc')
    if '1' in selected:
        commands.update({'ssh', 'tshark', 'chronyc'})
    if '3' in selected and args.role == 'publisher':
        commands.add('tc')
    for command in sorted(commands):
        check(command, bool(shutil.which(command)))
    result = subprocess.run(['env', '--default-signal=INT,QUIT', 'true'], capture_output=True)
    check('GNU env signal reset for background observers', result.returncode == 0)
    if 'nc' in commands and shutil.which('nc'):
        help_text = subprocess.run(['nc', '-h'], capture_output=True, text=True).stderr
        check('OpenBSD netcat (-N)', '-N' in help_text)

    packages = {
        '1': ('rclcpp_discovery_traffic', ['discovery_test']),
        '2': ('rp_exp_overhead', ['stress_pub', 'stress_sub']),
        '3': ('rp_exp_fidelity', ['drop_image_pub', 'drop_image_sub']),
        '4': ('rp_exp_probe_effect', ['s2_pub', 's2_sub', 's2_pub_rel', 's2_sub_rel']),
    }
    from ament_index_python.packages import get_package_prefix, PackageNotFoundError
    for key in sorted(selected):
        package, executables = packages[key]
        try:
            prefix = Path(get_package_prefix(package))
            check(package, all(os.access(prefix / 'lib' / package / name, os.X_OK) for name in executables), str(prefix))
        except PackageNotFoundError:
            check(package, False, 'build and source install/setup.bash')

    if args.role == 'receiver':
        rp = os.environ.get('RP_BIN') or str(ROOT / '.tools/bin/rp')
        check('rp executable', bool(rp and os.access(rp, os.X_OK)), rp)
        if Path(rp).resolve() == (ROOT / '.tools/bin/rp').resolve():
            try:
                manifest = installed_manifest()
                check('bundled rp source/build identity', True,
                      '2 MiB ring per interface; SHA-256 ' + manifest['binary_sha256'])
            except (OSError, ValueError, RuntimeError) as error:
                check('bundled rp source/build identity', False, str(error))
        if rp and os.access(rp, os.X_OK):
            for command in [('run',), ('topic', 'hz'), ('bag', 'record')]:
                result = subprocess.run([rp, *command, '--help'], capture_output=True, timeout=10)
                check('rp ' + ' '.join(command) + ' CLI', result.returncode == 0)
        if selected & {'2', '3', '4'}:
            try:
                rosbag = importlib.import_module('rosbag2_py')
                check('MCAP storage writer', 'mcap' in rosbag.get_registered_writers())
                importlib.import_module('rclpy.serialization')
                importlib.import_module('sensor_msgs.msg')
            except (ImportError, AttributeError) as error:
                check('ROS bag Python dependencies', False, str(error))

    if args.nic:
        check('NIC RX counter', os.access(f'/sys/class/net/{args.nic}/statistics/rx_bytes', os.R_OK), args.nic)
    if os.environ.get('RP_EXP_DATA_ADDRESS'):
        for name in ('rmem_max', 'wmem_max'):
            path = Path('/proc/sys/net/core') / name
            value = int(path.read_text()) if path.is_file() else 0
            check('Fast DDS UDP ' + name, value >= 65500,
                  f'{value} bytes; the experiment UDP transport requires at least 65500')
    if os.environ.get('RP_SOCKET', '/tmp/ros2probe.sock') != '/tmp/ros2probe.sock':
        check('rp command socket', False, 'the current rp CLI uses /tmp/ros2probe.sock')
    if args.skip_sudo:
        print('[SKIP] sudo/eBPF privileges (--skip-sudo); real observer execution remains unverified')
    elif shutil.which('sudo'):
        result = subprocess.run(['sudo', '-n', 'true'], capture_output=True, text=True)
        check('noninteractive sudo', result.returncode == 0,
              'available' if result.returncode == 0 else 'prepare remote experiment permissions with scripts/setup_testbed.py on the master')
    print('Checks are dependency checks; two-host routing, clock sync, eBPF attachment and measurements need a real run.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
