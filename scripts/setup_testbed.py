#!/usr/bin/env python3
"""Prepare master and two slaves using a private JSON file or interactive credentials."""
import argparse
from datetime import datetime, timezone
import fcntl
import getpass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from install_rp import source_identity
from bootstrap_host import validate_ros_platform
from run_experiments import deploy, execute
from ssh_access import prepare_access, prepare_local_client, ssh_options
from testbed import ROOT, ROLES, credentials, default_config, label, load, request


def logged(argv, path, password=None, timeout=1800):
    with path.open('w') as output:
        options = dict(stdout=output, stderr=subprocess.STDOUT, timeout=timeout, text=True)
        if password is None:
            options['stdin'] = subprocess.DEVNULL
        else:
            # SSH protects the stream; sudo -S consumes it. Never place it in argv/env/logs.
            options['input'] = password + '\n'
        result = subprocess.run(argv, **options)
    if result.returncode:
        raise RuntimeError(f'Setup command exited {result.returncode}; inspect {path}')


def prepare_transport(host, info, distro, password, log):
    # The repository and rsync may both be absent. Send only this stdlib installer
    # as Python code over SSH; sudo consumes its password from stdin before apt starts.
    source = (ROOT / 'scripts/bootstrap_host.py').read_text()
    argv = ['sudo', '-S', '-p', '', '--', 'python3', '-c', source, '--role', 'slave',
            '--user', info['user'], '--repo', info['repo'], '--ros-distro', distro,
            '--transport-only']
    logged(['ssh', *ssh_options(), host['ssh'], shlex.join(argv)], log, password=password)


def build_and_check(config_path, link, run_id, actions=('build', 'sync-clocks', 'check')):
    runner = [sys.executable, str(ROOT / 'scripts/run_experiments.py'),
              '--config', str(Path(config_path).resolve()), '--link', link]
    for action in actions:
        subprocess.run([*runner, action, '--run-id', run_id + '-' + action], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--config', type=Path, default=default_config())
    parser.add_argument('--link', choices=['wired', 'wireless'], help='installation SSH link from the single-file config')
    parser.add_argument('--register-ssh-key', action='store_true',
                        help='use the configured SSH password to register the project key even if another key works')
    parser.add_argument('--skip-master', action='store_true', help='master host preparation already completed')
    parser.add_argument('--same-sudo-password', action='store_true', help='prompt once for both slaves')
    parser.add_argument('--require-ssh-ready', action='store_true',
                        help='verify existing SSH access without registering keys')
    after = parser.add_mutually_exclusive_group()
    after.add_argument('--build-and-check', action='store_true',
                        help='after setup, build all workloads, synchronize clocks and check all experiments')
    after.add_argument('--build-workloads', action='store_true',
                       help='after setup, build all workloads while keeping Internet interfaces active')
    parser.add_argument('--run-id', type=label,
                        default='setup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    parser.add_argument('--dry-run', action='store_true', help='show scope without SSH, password prompts or writes')
    args = parser.parse_args()
    if args.require_ssh_ready and args.register_ssh_key:
        parser.error('--require-ssh-ready cannot be combined with --register-ssh-key')
    config = load(args.config, args.link)
    secrets = credentials(args.config)
    source = source_identity()
    if args.dry_run:
        print(json.dumps(dict(config=config, rp_source='vendor/ros2probe',
                              rp_source_sha256=source['source_sha256'],
                              capture_ring_bytes_per_interface=source['capture_ring_bytes_per_interface'],
                              prepare_master=not args.skip_master,
                              slave_ros_installation='automatic ROS base, Fast DDS, CLI and MCAP packages',
                              build_and_check=args.build_and_check,
                              build_workloads=args.build_workloads or args.build_and_check,
                              require_ssh_ready=args.require_ssh_ready,
                              actions=[('verify previously prepared SSH without registering keys' if args.require_ssh_ready
                                        else 'verify SSH or register a key using configured password'),
                                       'validate Ubuntu/ROS compatibility', 'prepare master dependencies unless skipped',
                                       'install remote rsync before deployment',
                                       'deploy source excluding private config', 'configure signed ROS apt repository if needed',
                                       'install ROS and experiment dependencies on slaves only', 'start chrony/SSH',
                                       'enable capture group', 'preserve host socket buffer settings',
                                       'cargo install bundled rp on each slave', 'install experiment command sudo rules',
                                       'verify remote privileges and CLI', 'close SSH connections; retain registered keys'],
                              passwords='private config when provided; otherwise interactive; excluded from this report'), indent=2))
        return 0
    (ROOT / 'results').mkdir(exist_ok=True)
    with (ROOT / 'results/.orchestrator.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another master operation is active; finish it before setup')
        output = ROOT / 'results' / args.run_id / '_master'
        output.mkdir(parents=True, exist_ok=False)
        manifest = dict(config=config, rp_source_sha256=source['source_sha256'],
                        status='incomplete', hosts={})
        shared_password = None
        try:
            prepare_local_client(secrets['master'])
            details = prepare_access(config, args.config, require_ready=args.require_ssh_ready,
                                     force_password=args.register_ssh_key)
            manifest['hosts'] = {role: dict(info, status='ssh-ready') for role, info in details.items()}
            for role in ROLES:
                distro = Path(config['hosts'][role]['ros_setup']).parent.name
                validate_ros_platform(distro, details[role]['os_release'], details[role]['arch'])
            if not args.skip_master:
                argv = [sys.executable, str(ROOT / 'scripts/bootstrap_host.py'),
                        '--role', 'master', '--user', getpass.getuser(), '--repo', str(ROOT)]
                if secrets['master']:
                    subprocess.run(['sudo', '-S', '-p', '', '--', *argv], check=True,
                                   input=secrets['master'] + '\n', text=True)
                else:
                    subprocess.run(['sudo', *argv], check=True)
            secrets['master'] = ''
            if args.same_sudo_password and any(not secrets['slaves'][r]['sudo'] for r in ROLES):
                shared_password = getpass.getpass('Sudo password for both slave laptops: ')
            for role in ROLES:
                secrets['slaves'][role]['sudo'] = (secrets['slaves'][role]['sudo'] or shared_password
                    or getpass.getpass(f'Sudo password for {role}: '))
                host, info = config['hosts'][role], details[role]
                print(f'[master] {role}: preparing source transfer', flush=True)
                prepare_transport(host, info, Path(host['ros_setup']).parent.name,
                                  secrets['slaves'][role]['sudo'], output / (role + '-transport.log'))
            deploy(config, args.config)
            for role in ROLES:
                host, info = config['hosts'][role], details[role]
                manifest['hosts'][role] = dict(info, status='incomplete')
                repo = Path(info['repo'])
                distro = Path(host['ros_setup']).parent.name
                argv = ['sudo', '-S', '-p', '', '--', 'python3', str(repo / 'scripts/bootstrap_host.py'),
                        '--role', 'slave', '--user', info['user'], '--repo', str(repo),
                        '--ros-distro', distro]
                password = secrets['slaves'][role]['sudo']
                print(f'[master] Preparing {role}; progress log: {output / (role + "-setup.log")}', flush=True)
                try:
                    logged(['ssh', *ssh_options(), host['ssh'], shlex.join(argv)],
                           output / (role + '-setup.log'), password=password, timeout=7200)
                finally:
                    password = None
                    secrets['slaves'][role]['sudo'] = ''
                verify = ['sudo -n true', 'setsid sudo -n true',
                          'id', 'sysctl net.core.wmem_max net.core.rmem_max',
                          'systemctl is-active chrony', 'getcap /usr/bin/dumpcap']
                logged(['ssh', *ssh_options(), host['ssh'], ' && '.join(verify)], output / (role + '-verify.log'), timeout=60)
                manifest['hosts'][role]['status'] = 'host-prepared'
                print(f'[master] {role}: dependencies and privileges prepared', flush=True)
            shared_password = None
            execute(config, {role: request(config, role, args.run_id, 'install-rp',
                ['python3', 'scripts/install_rp.py', '--setup-toolchain'], timeout=7200,
                workspace=False, reserve=True) for role in ROLES}, output)
            for role in ROLES:
                host = config['hosts'][role]
                repo = Path(details[role]['repo'])
                command = shlex.join(['python3', str(repo / 'scripts/install_rp.py'), '--verify'])
                build = json.loads(subprocess.check_output(['ssh', *ssh_options(), host['ssh'], command],
                                                          text=True, timeout=30))
                if build['source_sha256'] != source['source_sha256']:
                    raise RuntimeError(f'{role}: source changed during setup; redeploy and rebuild')
                (output / (role + '-rp-build.json')).write_text(json.dumps(build, indent=2) + '\n')
                manifest['hosts'][role].update(status='complete', rp_sha256=build['binary_sha256'])
                print(f'[master] {role}: source-built rp verified', flush=True)
            manifest['status'] = 'complete'
        except BaseException as exc:
            manifest['status'] = 'failed'
            manifest['error'] = type(exc).__name__ + ': ' + str(exc)
            raise
        finally:
            shared_password = None
            secrets = None
            (output / 'setup.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'[master] Setup complete: {output / "setup.json"}', flush=True)
    # Each master stage acquires the same orchestrator lock; setup must release it first.
    if args.build_and_check:
        build_and_check(args.config, config['link'], args.run_id)
    elif args.build_workloads:
        build_and_check(args.config, config['link'], args.run_id, actions=('build',))
        print('Dependencies and workloads ready. Next: ./run_exp1.sh (or another experiment launcher)', flush=True)
    else:
        print('Next: python3 scripts/run_experiments.py build', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
