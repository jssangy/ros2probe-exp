#!/usr/bin/env python3
"""One-time Ubuntu host preparation, invoked by setup_testbed.py with sudo."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import shutil
import subprocess
import tempfile
import time

ROS_PLATFORMS = {'humble': ('22.04', 'jammy'), 'jazzy': ('24.04', 'noble')}
# Official ros-infrastructure/ros-apt-source release; pin the bootstrap .deb and digest.
ROS_APT_VERSION = '1.3.0'
ROS_APT_SHA256 = {
    'jammy': '110b9a462d55252decb8b7c816f61c2ba0d9890ce5fb93ac504e97cae5860d76',
    'noble': 'f31d84adf5054c7d60ded0e82c0f776a77ea33af53d08f40b9ad7c94cca55296',
}


def run(argv, **options):
    print('[setup] ' + ' '.join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **options)


def missing_packages(packages):
    result = subprocess.run(['dpkg-query', '-W', '-f=${Package}\t${db:Status-Status}\n', *packages],
                            capture_output=True, text=True)
    installed = {parts[0] for line in result.stdout.splitlines()
                 if len(parts := line.split('\t')) == 2 and parts[1] == 'installed'}
    return [name for name in packages if name not in installed]


def install_packages(packages, env, no_remove=False):
    missing = missing_packages(packages)
    if missing:
        run(['apt-get', 'update'], env=env, stdin=subprocess.DEVNULL)
        run(['apt-get', 'install', '-y', *(['--no-remove'] if no_remove else []), *missing],
            env=env, stdin=subprocess.DEVNULL)
    else:
        print(f'[setup] All {len(packages)} required packages already installed; skipping APT', flush=True)


def validate_ros_platform(distro, release=None, arch=None):
    release = platform.freedesktop_os_release() if release is None else release
    arch = platform.machine() if arch is None else arch
    if distro not in ROS_PLATFORMS:
        raise ValueError('Supported ROS distros: humble, jazzy')
    version, codename = ROS_PLATFORMS[distro]
    if (release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != version
            or release.get('UBUNTU_CODENAME', release.get('VERSION_CODENAME')) != codename):
        raise ValueError(f'ROS 2 {distro} requires Ubuntu {version} ({codename}); '
                         'select the matching ros_distro in the testbed JSON')
    if arch not in ('x86_64', 'aarch64'):
        raise ValueError('Automatic ROS installation supports amd64 and arm64 Ubuntu only')
    return codename


def ros_packages(distro):
    return [f'ros-{distro}-' + name for name in
            ('ros-base', 'rclcpp', 'rclpy', 'sensor-msgs', 'geometry-msgs', 'std-msgs',
             'ament-cmake', 'launch-ros', 'ros2launch', 'rmw-fastrtps-cpp',
             'ros2topic', 'ros2bag', 'rosbag2-py', 'rosbag2-storage-mcap')]


def existing_ros_source(apt_root=Path('/etc/apt')):
    paths = [apt_root / 'sources.list', *(apt_root / 'sources.list.d').glob('*.list'),
             *(apt_root / 'sources.list.d').glob('*.sources')]
    for path in paths:
        if not path.is_file():
            continue
        active = '\n'.join(line for line in path.read_text().splitlines()
                           if not line.lstrip().startswith('#'))
        entries = re.split(r'\n\s*\n', active) if path.suffix == '.sources' else active.splitlines()
        for entry in entries:
            if path.suffix == '.sources':
                if re.search(r'^Enabled:\s*no\s*$', entry, re.M | re.I):
                    continue
                types = re.search(r'^Types:\s*(.+)$', entry, re.M)
                if not types or 'deb' not in types.group(1).split():
                    continue
            elif not entry.lstrip().startswith('deb '):
                continue
            if re.search(r'https?://packages\.ros\.org/ros2/ubuntu(?:\s|/|$)', entry):
                return True
    return False


def prepare_ros_repository(distro, env, report):
    """Only provision apt when ROS packages are missing; preserve existing ROS sources."""
    codename = validate_ros_platform(distro)
    report.update(distro=distro, ubuntu_codename=codename, locale='C.UTF-8')
    if not missing_packages(ros_packages(distro)):
        report['repository'] = 'unchanged; required ROS packages already installed'
        return
    install_packages(['ca-certificates', 'curl', 'software-properties-common'], env)
    run(['add-apt-repository', '--yes', '--no-update', 'universe'], env=env, stdin=subprocess.DEVNULL)
    if existing_ros_source():
        report['repository'] = 'existing ROS apt source preserved'
    else:
        with tempfile.TemporaryDirectory(prefix='ros2probe-ros-apt-') as directory:
            deb = Path(directory) / 'ros2-apt-source.deb'
            url = ('https://github.com/ros-infrastructure/ros-apt-source/releases/download/'
                   f'{ROS_APT_VERSION}/ros2-apt-source_{ROS_APT_VERSION}.{codename}_all.deb')
            run(['curl', '--fail', '--location', '--silent', '--show-error', '--retry', '3',
                 '--connect-timeout', '15', '--max-time', '180', '--proto', '=https',
                 '--output', deb, url], env=env, stdin=subprocess.DEVNULL)
            digest = hashlib.sha256(deb.read_bytes()).hexdigest()
            if digest != ROS_APT_SHA256[codename]:
                raise RuntimeError('ROS apt bootstrap SHA-256 mismatch; package was not installed')
            run(['dpkg', '--install', deb], env=env, stdin=subprocess.DEVNULL)
        report.update(repository='official ros2-apt-source', bootstrap_version=ROS_APT_VERSION,
                      bootstrap_sha256=digest)
    if distro == 'humble':
        # Early Jammy systemd/udev versions can conflict with ROS dependencies (ROS 2 #1272).
        critical = ['systemd', 'systemd-sysv', 'libsystemd0', 'udev', 'libudev1']
        absent = missing_packages(critical)
        installed = [name for name in critical if name not in absent]
        if installed:
            run(['apt-get', 'update'], env=env, stdin=subprocess.DEVNULL)
            run(['apt-get', 'install', '-y', '--only-upgrade', '--no-remove', *installed],
                env=env, stdin=subprocess.DEVNULL)


def verify_ros(distro, env):
    setup = Path(f'/opt/ros/{distro}/setup.bash')
    if not setup.is_file():
        raise RuntimeError(f'ROS package installation did not create {setup}')
    code = 'import rclpy, rosbag2_py; print("ROS Python imports: OK")'
    script = (f'set -e; source {shlex.quote(str(setup))}; '
              + shlex.join(['/usr/bin/python3', '-c', code])
              + '; ros2 --help >/dev/null; ros2 pkg prefix rmw_fastrtps_cpp; '
              'ros2 pkg prefix rosbag2_storage_mcap')
    run(['bash', '--noprofile', '--norc', '-c', script],
        env=dict(env, RMW_IMPLEMENTATION='rmw_fastrtps_cpp'), stdin=subprocess.DEVNULL)


def sudo_policy(user, repo, role):
    commands = [shutil.which('true'), shutil.which('chronyc') + ' *']
    if role == 'slave':
        commands += [shutil.which('kill') + ' *', shutil.which('tc') + ' *',
                     str(repo / '.tools/bin/rp') + ' run',
                     shutil.which('python3') + ' ' + str(repo / 'scripts/network_guard.py') + ' --root *',
                     shutil.which('python3') + ' ' + str(repo / 'common/sample_resources.py') + ' *',
                     shutil.which('tee') + ' /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor',
                     shutil.which('tee') + ' /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq',
                     shutil.which('rm') + ' -rf -- ' + str(repo / 'bags') + '/*',
                     shutil.which('rm') + ' -rf -- ' + str(repo / 'results') + '/*']
        for executable, arguments in [('modprobe', 'cpufreq_performance'),
                                      ('cpupower', 'frequency-set -g performance')]:
            if shutil.which(executable):
                commands.append(shutil.which(executable) + ' ' + arguments)
    alias = 'ROS2PROBE_EXP_' + re.sub('[^A-Z0-9]', '_', user.upper())
    return ('# Managed experiment commands; passwords are not stored here.\n'
            f'Cmnd_Alias {alias} = ' + ', '.join(commands) + '\n'
            f'{user} ALL=(root) NOPASSWD: {alias}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['master', 'slave'], required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--ros-distro', choices=['humble', 'jazzy'], default='jazzy')
    parser.add_argument('--transport-only', action='store_true',
                        help='prepare slave rsync before the first repository deployment')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run this one-time host setup with sudo')
    account = pwd.getpwnam(args.user)
    repo = args.repo.resolve()
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user):
        parser.error('Unsupported account name')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(repo)) or repo in (Path('/'), Path(account.pw_dir)):
        parser.error('Use a dedicated repository path without spaces for automatic sudo rules')
    if args.role == 'slave':
        validate_ros_platform(args.ros_distro)
    if args.transport_only:
        if args.role != 'slave':
            parser.error('--transport-only is for slave preparation')
        install_packages(['rsync'], dict(os.environ, DEBIAN_FRONTEND='noninteractive',
                                        NEEDRESTART_MODE='a', LC_ALL='C.UTF-8'))
        return
    if not (repo / 'scripts/run_experiments.py').is_file() or repo.stat().st_uid != account.pw_uid:
        parser.error('Repository must exist and be owned by the experiment account')
    if args.role == 'slave':
        if not (repo / 'vendor/ros2probe/ros2probe/Cargo.toml').is_file():
            parser.error('Deploy the bundled ros2probe sources before host preparation')

    (repo / 'results').mkdir(exist_ok=True)
    os.chown(repo / 'results', account.pw_uid, account.pw_gid)
    lock_name = '.orchestrator.lock' if args.role == 'master' else '.master.lock'
    with (repo / 'results' / lock_name).open('a') as lock:
        os.chown(lock.name, account.pw_uid, account.pw_gid)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('An experiment job is active; finish it before host setup')
        backup = Path('/var/lib/ros2probe-exp-setup') / (time.strftime('%Y%m%dT%H%M%S') + '-' + args.user)
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        report = dict(role=args.role, user=args.user, repo=str(repo), status='incomplete',
                      backup=str(backup), ros_distro=args.ros_distro)

        def replace_config(path, content, mode=0o644):
            if path.exists():
                saved = backup / path.relative_to('/')
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp.write(content.encode())
            os.chmod(temp.name, mode)
            os.replace(temp.name, path)

        try:
            env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='a', LC_ALL='C.UTF-8')
            # No password remains on stdin: every child receives either DEVNULL or explicit data.
            packages = ['python3', 'python3-numpy', 'python3-matplotlib', 'chrony',
                        'openssh-client', 'rsync', 'iproute2', 'sudo']
            if args.role == 'slave':
                report['ros'] = {}
                if missing_packages(ros_packages(args.ros_distro)):
                    for path in [Path('/etc/apt/sources.list'), Path('/etc/apt/sources.list.d')]:
                        if path.exists():
                            saved = backup / path.relative_to('/')
                            saved.parent.mkdir(parents=True, exist_ok=True)
                            if path.is_dir():
                                shutil.copytree(path, saved, symlinks=True)
                            else:
                                shutil.copy2(path, saved)
                prepare_ros_repository(args.ros_distro, env, report['ros'])
                packages += ['build-essential', 'cmake', 'git', 'curl', 'ca-certificates',
                             'pkg-config', 'clang', 'llvm', 'libelf-dev', 'libssl-dev', 'libgtk-3-dev',
                             'python3-colcon-common-extensions', 'netcat-openbsd', 'procps',
                             'util-linux', 'coreutils', 'openssh-server', 'tshark', 'libcap2-bin', 'network-manager']
                run(['debconf-set-selections'], input='wireshark-common wireshark-common/install-setuid boolean true\n', text=True)
            install_packages(packages, env)
            if args.role == 'slave':
                ros = ros_packages(args.ros_distro)
                install_packages(ros, env, no_remove=True)
                packages += ros
                verify_ros(args.ros_distro, env)
                report['ros']['status'] = 'verified'
            versions = subprocess.check_output(['dpkg-query', '-W', '-f=${Package}\t${Version}\n', *packages], text=True)
            report['package_versions'] = dict(line.split('\t', 1) for line in versions.splitlines())
            run(['systemctl', 'enable', '--now', 'chrony'], stdin=subprocess.DEVNULL)
            if args.role == 'slave':
                run(['systemctl', 'enable', '--now', 'ssh'], stdin=subprocess.DEVNULL)
                run(['dpkg-reconfigure', '-f', 'noninteractive', 'wireshark-common'], env=env, stdin=subprocess.DEVNULL)
                run(['usermod', '-aG', 'wireshark', args.user], stdin=subprocess.DEVNULL)
                # Buffer policy belongs to the host; setup must not tune it.
                report['socket_buffer_policy'] = 'preserve-host-settings'
                report['socket_buffers'] = {
                    name: int((Path('/proc/sys/net/core') / name).read_text())
                    for name in ('wmem_max', 'wmem_default', 'rmem_max', 'rmem_default')}
                report['rp_installation'] = 'cargo install as experiment user after host preparation'

            policy = sudo_policy(args.user, repo, args.role)
            policy_path = Path('/etc/sudoers.d') / ('90-ros2probe-exp-' + args.user)
            candidate = backup / 'sudoers-candidate'
            candidate.write_text(policy)
            os.chmod(candidate, 0o440)
            run(['visudo', '-cf', candidate], stdin=subprocess.DEVNULL)
            replace_config(policy_path, policy, 0o440)
            run(['visudo', '-c'], stdin=subprocess.DEVNULL)
            report['sudoers_file'] = str(policy_path)
            report['status'] = 'complete'
        finally:
            (backup / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('[setup] Complete: ' + str(backup / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
