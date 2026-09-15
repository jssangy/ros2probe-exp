"""Public launchers preserve link isolation, credentials and setup-stage boundaries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import setup_testbed
import ssh_access
import run_experiments as master
import testbed


class Launchers(unittest.TestCase):
    def fixture(self, directory):
        path = Path(directory) / 'private testbed.json'
        document = json.loads((ROOT / 'testbed.example.json').read_text())
        document['default_link'] = 'wireless'
        for slave in document['slaves'].values():
            slave['password'] = 'fixture-launcher-private-5701'
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        return path

    def invoke(self, name, path, *args):
        return subprocess.run([str(ROOT / name), '--config', str(path), *args],
                              cwd=path.parent, capture_output=True, text=True, timeout=10)

    def test_experiment_launchers_select_their_own_link_from_any_directory(self):
        cases = [('run_exp1.sh', '1', 'wired'), ('run_exp2.sh', '2', 'wired'),
                 ('run_exp3.sh', '3', 'wired'), ('run_exp4_wired.sh', '4', 'wired'),
                 ('run_exp4_wireless.sh', '4', 'wireless')]
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            before = path.read_bytes(), path.stat().st_mtime_ns
            ids = []
            for name, experiment, link in cases:
                with self.subTest(name=name):
                    result = self.invoke(name, path, '--dry-run')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn('fixture-launcher-private-5701', result.stdout + result.stderr)
                    data = json.loads(result.stdout)
                    self.assertEqual(data['config']['link'], link)
                    self.assertTrue(data['prepare_ssh'])
                    self.assertTrue(data['config']['_network_guard'])
                    if experiment == '1':
                        self.assertEqual(len(data['stages']), 90)
                        self.assertEqual({stage['jobs']['slave_b']['argv'][
                            stage['jobs']['slave_b']['argv'].index('--N_P') + 1] for stage in data['stages']}, {'1', '3', '5'})
                        self.assertEqual({stage['name'].rsplit('-', 1)[1] for stage in data['stages']},
                                         {f'{rep:02d}' for rep in range(1, 11)})
                    else:
                        for stage in data['stages']:
                            argv = stage['jobs']['slave_b']['argv']
                            self.assertEqual(argv[argv.index('--runs') + 1], '10')
                            if experiment == '2':
                                self.assertEqual(argv[argv.index('--scenarios') + 1], 'ST100,ST500,ST1000')
                            elif experiment == '3':
                                self.assertEqual(argv[argv.index('--losses') + 1], '0,10,20')
                            elif experiment == '4':
                                self.assertEqual(argv[argv.index('--scenarios') + 1], 'S1,S2,S3,S4,S5,S6,S7')
                    if experiment == '4':
                        self.assertEqual(len(data['stages']), 2)
                        self.assertEqual({stage['jobs']['slave_b']['argv'][
                            stage['jobs']['slave_b']['argv'].index('--qos') + 1] for stage in data['stages']},
                            {'best_effort', 'reliable'})
                        self.assertEqual(len({job['runs_csv'] for job in data['analysis']}), 2)
                    for job in data['preflight_or_build'].values():
                        argv = job['argv']
                        self.assertEqual(argv[argv.index('--experiment') + 1], experiment)
                        self.assertEqual(job['env']['NIC'], 'eno1' if link == 'wired' else 'wlp4s0')
                    target = Path(data['collect_to'])
                    self.assertFalse(target.exists())
                    self.assertTrue(data['analysis'])
                    ids.append(target.name)
            self.assertEqual(len(set(ids)), 5)
            self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_dependency_launcher_builds_without_clock_or_interface_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            result = self.invoke('install_dependencies.sh', path, '--skip-master', '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertFalse(data['require_ssh_ready'])
            self.assertEqual(data['config']['link'], 'wired')
            self.assertTrue(data['build_workloads'])
            self.assertFalse(data['build_and_check'])
            self.assertNotIn('fixture-launcher-private-5701', result.stdout)
        with patch.object(setup_testbed.subprocess, 'run') as run:
            setup_testbed.build_and_check('/private.json', 'wired', 'deps', actions=('build',))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-3], 'build')

    def test_missing_ssh_stops_installation_before_master_privilege_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            argv = ['setup_testbed.py', '--config', str(path), '--build-workloads', '--run-id', 'ssh-failure']
            with patch.object(sys, 'argv', argv), \
                 patch.object(setup_testbed, 'ROOT', Path(directory)), \
                 patch.object(setup_testbed, 'prepare_local_client'), \
                 patch.object(setup_testbed, 'prepare_access', side_effect=RuntimeError('SSH failed')), \
                 patch.object(setup_testbed.subprocess, 'run') as run, \
                 self.assertRaisesRegex(RuntimeError, 'SSH failed'):
                setup_testbed.main()
            run.assert_not_called()
            report = json.loads((Path(directory) / 'results/ssh-failure/_master/setup.json').read_text())
            self.assertEqual(report['status'], 'failed')

    def test_readonly_ssh_check_cannot_register_keys_or_accept_new_host_keys(self):
        with patch.object(ssh_access.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run, \
             self.assertRaisesRegex(RuntimeError, 'install_dependencies.sh'):
            ssh_access.check_access(dict(ssh='operator@192.0.2.2'))
        self.assertIn('StrictHostKeyChecking=yes', run.call_args.args[0])
        self.assertNotIn('env', run.call_args.kwargs)

    def test_preparation_only_contacts_selected_link(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            for link in ('wired', 'wireless'):
                config = testbed.load(path, link)
                with self.subTest(link=link), \
                     patch.object(ssh_access, 'ensure_access', return_value='existing-key') as auth, \
                     patch.object(ssh_access, 'host_info', side_effect=[{'machine_id': 'A'}, {'machine_id': 'B'}]) as info:
                    details = ssh_access.prepare_access(config, path)
                self.assertEqual([c.args[0] for c in auth.call_args_list], list(config['hosts'].values()))
                self.assertEqual([c.args[0] for c in info.call_args_list], list(config['hosts'].values()))
                self.assertEqual(set(details), {'slave_a', 'slave_b'})

    def test_ssh_failure_cannot_start_workers_isolate_or_collect(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                path = self.fixture(directory)
                cli = master.parser()
                args = cli.parse_args(['--config', str(path), '--link', 'wired', 'run', '1',
                                       '--run-id', 'flow', '--prepare-ssh'])
                events = []

                def prepare(*_):
                    events.append('ssh')
                    if fail:
                        raise RuntimeError('fixture SSH failure')
                    return {}

                with patch.object(master, 'ROOT', Path(directory)), \
                     patch.object(master, 'prepare_access', side_effect=prepare), \
                     patch.object(master, 'stages', return_value=[]), \
                     patch.object(master, 'preflight', return_value={}), \
                     patch.object(master, 'export_jobs', return_value=[]), \
                     patch.object(master, 'execute') as execute, \
                     patch.object(master, 'NetworkSession') as network, \
                     patch.object(master, 'synchronize_clocks', side_effect=lambda *a, **k: events.append('sync')), \
                     patch.object(master, 'collect', side_effect=lambda *a: events.append('collect')) as collect, \
                     patch.object(master, 'analyze_collected', side_effect=lambda *a: events.append('analysis')):
                    network.return_value.start.side_effect = lambda: events.append('isolate')
                    network.return_value.close.side_effect = lambda: events.append('restore')
                    code = master.run_main(cli, args)
                    if fail:
                        network.assert_not_called()
                        execute.assert_not_called()
                        collect.assert_not_called()
                self.assertEqual(code, int(fail))
                self.assertEqual(events, ['ssh'] if fail else ['ssh', 'isolate', 'sync', 'restore', 'collect', 'analysis'])
                report = json.loads((Path(directory) / 'results/flow/_master/run.json').read_text())
                self.assertEqual(report['ssh_status'], 'failed' if fail else 'complete')
                if fail:
                    self.assertEqual(report['collection_status'], 'not-started')
                    self.assertNotIn('network_restoration_status', report)

    def test_user_ssh_config_cannot_leave_persistent_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'ssh_config'
            config.write_text('Host *\n ControlMaster auto\n ControlPath /tmp/fixture-control-%h\n ControlPersist 10m\n')
            for options in (ssh_access.ssh_options(), ssh_access.password_options()):
                result = subprocess.run(['ssh', '-G', '-F', str(config), *options, '192.0.2.2'],
                                        capture_output=True, text=True, check=True)
                settings = dict(line.split(' ', 1) for line in result.stdout.splitlines())
                self.assertEqual(settings['controlmaster'], 'false')
                self.assertEqual(settings['controlpersist'], 'no')
                self.assertNotIn('controlpath', settings)

    def test_explicit_run_id_and_measurement_options_reach_the_shared_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            result = self.invoke('run_exp4_wireless.sh', path, '--dry-run', '--run-id', 'launcher-fixed',
                                 '--probe-scenarios', 'S2', '--qos', 'reliable', '--runs', '2')
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(Path(data['collect_to']).name, 'launcher-fixed')
            argv = data['stages'][0]['jobs']['slave_b']['argv']
            for key, value in [('--scenarios', 'S2'), ('--qos', 'reliable'), ('--runs', '2')]:
                self.assertEqual(argv[argv.index(key) + 1], value)

    def test_wrong_link_cannot_override_the_named_experiment_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.fixture(directory)
            result = self.invoke('run_exp4_wired.sh', path, '--dry-run', '--link', 'wireless')
            self.assertNotEqual(result.returncode, 0)
            result = self.invoke('run_exp1.sh', path, '--dry-run', '--np', '2')
            self.assertNotEqual(result.returncode, 0)
            for option in ('--link', '--li', '--link=wireless'):
                with self.subTest(option=option):
                    args = [option] if '=' in option else [option, 'wireless']
                    result = self.invoke('install_dependencies.sh', path, '--dry-run', *args)
                    self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
