"""Fresh-host ROS setup, early platform rejection and unattended setup ordering."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bootstrap_host as host
import setup_testbed as setup


class RosBootstrap(unittest.TestCase):
    def test_incompatible_ros_os_and_arch_fail_before_installation(self):
        noble = dict(ID='ubuntu', VERSION_ID='24.04', VERSION_CODENAME='noble')
        self.assertEqual(host.validate_ros_platform('jazzy', noble, 'x86_64'), 'noble')
        with self.assertRaisesRegex(ValueError, '22.04'):
            host.validate_ros_platform('humble', noble, 'x86_64')
        with self.assertRaisesRegex(ValueError, 'amd64 and arm64'):
            host.validate_ros_platform('jazzy', noble, 'armv7l')
        with self.assertRaises(ValueError):
            host.validate_ros_platform('jazzy', dict(noble, ID='debian'), 'x86_64')

    def test_equipped_slave_does_not_download_or_edit_sources(self):
        report = {}
        with patch.object(host, 'validate_ros_platform', return_value='noble'), \
             patch.object(host, 'missing_packages', return_value=[]), \
             patch.object(host, 'install_packages') as install, patch.object(host, 'run') as run:
            host.prepare_ros_repository('jazzy', {}, report)
        install.assert_not_called()
        run.assert_not_called()
        self.assertIn('unchanged', report['repository'])

    def test_fresh_host_uses_pinned_verified_official_repository(self):
        payload = b'fixture ROS apt package'
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'curl': Path(argv[argv.index('--output')+1]).write_bytes(payload)
        report = {}
        with patch.object(host, 'validate_ros_platform', return_value='noble'), \
             patch.object(host, 'missing_packages', side_effect=lambda packages: packages), \
             patch.object(host, 'existing_ros_source', return_value=False), \
             patch.object(host, 'install_packages'), patch.object(host, 'run', side_effect=run), \
             patch.dict(host.ROS_APT_SHA256, noble=hashlib.sha256(payload).hexdigest()):
            host.prepare_ros_repository('jazzy', {}, report)
        self.assertEqual(calls[0], ['add-apt-repository', '--yes', '--no-update', 'universe'])
        self.assertIn('https://github.com/ros-infrastructure/ros-apt-source/releases/download/', calls[1][-1])
        self.assertEqual(calls[2][:2], ['dpkg', '--install'])
        self.assertEqual(report['repository'], 'official ros2-apt-source')
        self.assertIn('ros-jazzy-ros-base', host.ros_packages('jazzy'))
        self.assertIn('ros-jazzy-rosbag2-storage-mcap', host.ros_packages('jazzy'))

    def test_changed_download_is_rejected_before_dpkg(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'curl': Path(argv[argv.index('--output')+1]).write_bytes(b'changed package')
        with patch.object(host, 'validate_ros_platform', return_value='noble'), \
             patch.object(host, 'missing_packages', return_value=['ros-jazzy-ros-base']), \
             patch.object(host, 'existing_ros_source', return_value=False), \
             patch.object(host, 'install_packages'), patch.object(host, 'run', side_effect=run), \
             self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
            host.prepare_ros_repository('jazzy', {}, {})
        self.assertFalse(any(argv[0] == 'dpkg' for argv in calls))

    def test_existing_ros_repository_is_not_duplicated(self):
        with patch.object(host, 'validate_ros_platform', return_value='noble'), \
             patch.object(host, 'missing_packages', return_value=['ros-jazzy-ros-base']), \
             patch.object(host, 'existing_ros_source', return_value=True), \
             patch.object(host, 'install_packages'), patch.object(host, 'run') as run:
            host.prepare_ros_repository('jazzy', {}, {})
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][0], 'add-apt-repository')

    def test_ros_install_cannot_remove_system_packages(self):
        with patch.object(host, 'missing_packages', return_value=['ros-humble-ros-base']), \
             patch.object(host, 'run') as run:
            host.install_packages(['ros-humble-ros-base'], {}, no_remove=True)
        self.assertIn('--no-remove', run.call_args_list[-1].args[0])

    def test_sources_detection_accepts_deb822_and_ignores_commented_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sources.list.d').mkdir()
            (root / 'sources.list').write_text('# deb https://packages.ros.org/ros2/ubuntu noble main\n')
            self.assertFalse(host.existing_ros_source(root))
            (root / 'sources.list.d/ros2.sources').write_text(
                'Types: deb\nURIs: http://packages.ros.org/ros2/ubuntu\nSuites: noble\nComponents: main\n')
            self.assertTrue(host.existing_ros_source(root))
            with (root / 'sources.list.d/ros2.sources').open('a') as source:
                source.write('Enabled: no\n')
            self.assertFalse(host.existing_ros_source(root))

    def test_humble_updates_installed_systemd_before_ros_dependencies(self):
        with patch.object(host, 'validate_ros_platform', return_value='jammy'), \
             patch.object(host, 'missing_packages', side_effect=[['ros-humble-ros-base'], ['udev']]), \
             patch.object(host, 'existing_ros_source', return_value=True), \
             patch.object(host, 'install_packages'), patch.object(host, 'run') as run:
            host.prepare_ros_repository('humble', {}, {})
        argv = run.call_args_list[-1].args[0]
        self.assertEqual(argv[:5], ['apt-get', 'install', '-y', '--only-upgrade', '--no-remove'])
        self.assertIn('systemd', argv)
        self.assertNotIn('udev', argv)

    def test_transport_does_not_require_predeployed_repo_or_rsync(self):
        info = dict(user='operator', repo='/home/operator/new-checkout')
        with patch.object(setup, 'logged') as logged:
            setup.prepare_transport(dict(ssh='operator@192.0.2.2'), info, 'jazzy',
                                    'fixture-secret', Path('/tmp/transport.log'))
        argv = logged.call_args.args[0]
        self.assertEqual(argv[0], 'ssh')
        self.assertIn('--transport-only', argv[-1])
        self.assertIn('python3 -c', argv[-1])
        self.assertNotIn('fixture-secret', str(argv))
        self.assertEqual(logged.call_args.kwargs['password'], 'fixture-secret')

    def test_build_then_clock_sync_then_check_and_stop_on_failure(self):
        with patch.object(setup.subprocess, 'run') as run:
            setup.build_and_check('/tmp/private.json', 'wireless', 'setup-trial')
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual([c[-3] for c in commands], ['build', 'sync-clocks', 'check'])
        self.assertTrue(all(c[2:6] == ['--config', '/tmp/private.json', '--link', 'wireless'] for c in commands))
        with patch.object(setup.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['build'])) as run, \
             self.assertRaises(subprocess.CalledProcessError):
            setup.build_and_check('/tmp/private.json', 'wired', 'setup-trial')
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
