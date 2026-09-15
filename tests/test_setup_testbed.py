"""Host preparation must not update unrelated software or persist credentials."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bootstrap_host
import setup_testbed
import install_rp


class Preparation(unittest.TestCase):
    def test_installed_dependencies_do_not_touch_apt(self):
        query = subprocess.CompletedProcess([], 0, 'chrony\tinstalled\nrsync\tinstalled\n', '')
        with patch.object(bootstrap_host.subprocess, 'run', return_value=query) as run:
            bootstrap_host.install_packages(['chrony', 'rsync'], {})
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][0], 'dpkg-query')

    def test_only_missing_packages_are_installed(self):
        query = subprocess.CompletedProcess([], 1, 'chrony\tinstalled\ntshark\tconfig-files\n', '')
        with patch.object(bootstrap_host.subprocess, 'run', return_value=query), \
                patch.object(bootstrap_host, 'run') as run:
            bootstrap_host.install_packages(['chrony', 'tshark', 'rsync'], {})
        self.assertEqual(run.call_args_list[0].args[0], ['apt-get', 'update'])
        self.assertEqual(run.call_args_list[1].args[0], ['apt-get', 'install', '-y', 'tshark', 'rsync'])

    def test_password_is_only_subprocess_input(self):
        secret = 'test-input-only'
        argv = ['ssh', 'slave', 'sudo -S -p "" -- true']
        result = subprocess.CompletedProcess(argv, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'setup.log'
            with patch.object(setup_testbed.subprocess, 'run', return_value=result) as run:
                setup_testbed.logged(argv, path, password=secret)
            self.assertEqual(run.call_args.kwargs['input'], secret + '\n')
            self.assertNotIn(secret, str(run.call_args.args))
            self.assertNotIn('env', run.call_args.kwargs)
            self.assertNotIn(secret, path.read_text())

    def test_master_policy_only_allows_clock_commands(self):
        policy = bootstrap_host.sudo_policy('operator', Path('/home/operator/ros2probe-exp'), 'master')
        self.assertIn('chronyc *', policy)
        self.assertNotIn('NOPASSWD: ALL', policy)
        self.assertNotIn('python', policy)
        self.assertNotIn('apt-get', policy)

    def test_prebuilt_argument_is_removed_and_dry_run_uses_bundled_source(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/setup_testbed.py'),
                                 '--rp-binary', '/tmp/rp'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unrecognized arguments', result.stderr)

    def test_stale_source_and_replaced_binary_fail_verification(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'vendor/ros2probe'
            (source / 'ros2probe/src/capture').mkdir(parents=True)
            (source / 'Cargo.lock').write_text('fixture lock')
            (source / 'ros2probe/Cargo.toml').write_text('fixture manifest')
            (source / 'ros2probe/src/capture/socket.rs').write_text(
                'const CAPTURE_RING_BLOCK_SIZE: usize = 256 * 1024;\n'
                'const CAPTURE_RING_BLOCK_COUNT: usize = 8;\n')
            binary = root / '.tools/bin/rp'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b'fixture source-built binary')
            manifest = dict(install_rp.source_identity(root), status='complete',
                            binary_sha256=install_rp.sha256(binary))
            (root / '.tools/rp-build.json').write_text(json.dumps(manifest))
            self.assertEqual(install_rp.installed_manifest(root)['status'], 'complete')
            binary.write_bytes(b'old 32 MiB binary')
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                install_rp.installed_manifest(root)
            binary.write_bytes(b'fixture source-built binary')
            (source / 'ros2probe/new.rs').write_text('new source file')
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                install_rp.installed_manifest(root)

    def test_missing_build_does_not_use_legacy_rp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.tools').mkdir()
            (root / '.tools/rp').write_bytes(b'previous installed binary')
            with self.assertRaisesRegex(RuntimeError, 'Build bundled rp first'):
                install_rp.installed_manifest(root)

    def test_sudo_policy_uses_cargo_install_location(self):
        policy = bootstrap_host.sudo_policy('operator', Path('/home/operator/ros2probe-exp'), 'slave')
        self.assertIn('/.tools/bin/rp run', policy)
        self.assertNotIn('/.tools/rp run', policy)


if __name__ == '__main__':
    unittest.main()
