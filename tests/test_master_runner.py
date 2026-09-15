"""Master topology, phase ordering, and real slave watchdog cancellation tests."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import testbed
import run_experiments as master


def configuration():
    return dict(domain_id=77, link='wired', master_clock_address='192.0.2.1', hosts={
        'slave_a': dict(ssh='alice@slave-a', address='192.0.2.10', nic='enp1s0',
                        repo='~/experiment a', ros_setup='/opt/ros/jazzy/setup.bash'),
        'slave_b': dict(ssh='bob@slave-b', address='192.0.2.20', nic='enp2s0',
                        repo='/data/experiment b', ros_setup='/opt/ros/jazzy/setup.bash')})


class Plans(unittest.TestCase):
    def test_exactly_two_distinct_slaves(self):
        self.assertEqual(testbed.validate(configuration())['domain_id'], 77)
        for mutation in ('missing', 'same_ip', 'same_ssh', 'bad_ssh', 'loopback'):
            config = configuration()
            if mutation == 'missing': del config['hosts']['slave_b']
            if mutation == 'same_ip': config['hosts']['slave_b']['address'] = '192.0.2.10'
            if mutation == 'same_ssh': config['hosts']['slave_b']['ssh'] = 'alice@slave-a'
            if mutation == 'bad_ssh': config['hosts']['slave_b']['ssh'] = '-oProxyCommand=bad'
            if mutation == 'loopback': config['hosts']['slave_b']['nic'] = 'lo'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                testbed.validate(config)

    def test_discovery_shared_epoch_per_slave_paths_and_nics(self):
        args = master.parser().parse_args(['run', '1', '--run-id', 'check', '--np', '1,3,5'])
        plan = testbed.stages(configuration(), args, 123456)
        self.assertEqual(len(plan), 90)
        for stage in plan:
            self.assertFalse(stage['paired'])
            self.assertEqual(stage['cooldown'], 25)
            for role, job in stage['jobs'].items():
                argv = job['argv']
                value = lambda flag: argv[argv.index(flag) + 1]
                self.assertEqual(value('--target-time'), '123456')
                self.assertEqual(int(value('--total-processes')), int(value('--N_P')) * 2)
                self.assertEqual(value('--run-duration'), '7')
                self.assertEqual(value('--capture-interface'), configuration()['hosts'][role]['nic'])
                self.assertIn('/' + role + '/', value('--capture-output'))
                self.assertEqual(job['env']['DISCOVERY_ROS_DOMAIN_ID'], '77')

    def test_other_experiments_keep_direct_slave_handshake_and_pc_platform(self):
        args = master.parser().parse_args(['run', 'all', '--run-id', 'check'])
        plan = testbed.stages(configuration(), args)
        pairs = [s for s in plan if s['paired']]
        self.assertEqual(len(pairs), 5)  # resource rate + bag, fidelity, both probe QoS profiles
        for stage in pairs:
            a, b = (stage['jobs'][role] for role in testbed.ROLES)
            self.assertEqual(a['argv'][a['argv'].index('--sync') + 1], '192.0.2.20')
            self.assertEqual(b['argv'][b['argv'].index('--sync') + 1], '192.0.2.10')
            for job in (a, b):
                self.assertEqual(job['argv'][job['argv'].index('--platform') + 1], 'pc')
                self.assertTrue((ROOT / job['argv'][1]).is_file())
            self.assertEqual(b['unused_ports'], [a['ready_port'] + 1])
            self.assertEqual(b['argv'][b['argv'].index('--runs') + 1], '10')
        analyses = testbed.analysis_jobs(configuration(), args)
        self.assertEqual([job['job_id'] for job in analyses],
                         ['analysis-1-slave_a', 'analysis-1-slave_b', 'analysis-2-slave_b',
                          'analysis-3-slave_b', 'analysis-4-slave_b-best_effort', 'analysis-4-slave_b-reliable'])
        self.assertTrue(all('repo' not in job for job in analyses))
        for job, peer in zip(analyses[:2], ('slave_b', 'slave_a')):
            argv = job['argv']
            self.assertIn('/'+peer+'/01_discovery', argv[argv.index('--peer-base-dir')+1])
        self.assertIn('--sequence-csv', analyses[3]['argv'])
        for stage in pairs:
            b = stage['jobs']['slave_b']
            self.assertTrue(b['master_schedule'])
            self.assertEqual(b['env']['RP_EXP_SCHEDULE_TRIGGER'], 'GO' if stage['name'].startswith('exp3') else 'START')

    def test_dry_run_has_no_ssh_or_output_creation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'testbed.json'
            path.write_text(json.dumps(configuration()))
            run_id = 'dry-run-must-not-create-output'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/run_experiments.py'),
                '--config', str(path), 'run', 'all', '--run-id', run_id, '--dry-run'],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(json.loads(result.stdout)['stages']), 95)
            self.assertFalse((ROOT / 'results' / run_id).exists())

    def test_publisher_readiness_and_peer_failure_cleanup(self):
        for mode in ('success', 'receiver_failure', 'early_publisher_exit'):
            created, stopped = [], []

            class FakeRemote:
                def __init__(self, host, job, output):
                    if created:
                        self_outer.assertTrue(created[0].ready, 'B started before A was ready')
                    self.role, self.job = host, job
                    self.identity = 'test-identity-' + str(len(created))
                    self.ready = False
                    self.code = None
                    self.ticks = 0
                    self.proc = self
                    created.append(self)

                def poll(self):
                    return self.code

                def tick(self):
                    self.ticks += 1
                    if len(created) == 1:
                        if mode == 'early_publisher_exit':
                            self.code = 0
                        else:
                            self.ready = self.ticks >= 3
                    else:
                        self.code = 17 if mode == 'receiver_failure' and self is created[1] else 0
                    return self.code

            self_outer = self
            args = master.parser().parse_args(['run', '4', '--run-id', 'check'])
            stage = testbed.stages(configuration(), args)[0]
            with self.subTest(mode=mode), patch.object(master, 'Remote', FakeRemote), \
                    patch.object(master, 'stop_all', lambda jobs: stopped.extend(jobs)), \
                    patch.object(master.time, 'sleep', lambda seconds: None):
                if mode == 'success':
                    master.execute(configuration(), stage['jobs'], Path('/unused'), paired=True)
                else:
                    with self.assertRaises(RuntimeError):
                        master.execute(configuration(), stage['jobs'], Path('/unused'), paired=True)
            self.assertEqual(len(created), 1 if mode == 'early_publisher_exit' else 2)
            self.assertEqual(stopped, created)

    def test_deploy_preserves_executable_modes_and_excludes_slave_data(self):
        with patch.object(master.subprocess, 'run') as run:
            master.deploy(configuration())
        transfers = [call.args[0] for call in run.call_args_list if call.args[0][0] == 'rsync']
        self.assertEqual(len(transfers), 2)
        for argv in transfers:
            self.assertIn('-rltp', argv)
            self.assertIn('--protect-args', argv)
            self.assertNotIn('--delete', argv)
            for tree in ('/results', '/bags', '/install', '/.tools', '/.testbed.json', '/.testbed.*.json'):
                self.assertIn(tree, argv)


class Watchdog(unittest.TestCase):
    def run_job(self, code, action=None, **options):
        with tempfile.TemporaryDirectory(prefix='master-watchdog-') as d:
            repo = Path(d)
            (repo / 'common').mkdir()
            (repo / 'common/environment.sh').write_text('rp_exp_source_setup() { source "$1"; }\n')
            (repo / 'setup.bash').write_text('true\n')
            job = dict(repo=d, ros_setup=str(repo / 'setup.bash'), run_id='test', job_id='worker',
                       argv=[sys.executable, '-u', '-c', code], timeout=5,
                       workspace=False, reserve=True, cleanup_grace=2, **options)
            encoded = base64.b64encode(json.dumps(job).encode()).decode()
            proc = subprocess.Popen([sys.executable, str(ROOT / 'scripts/remote_job.py'), encoded],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                proc.stdin.write(b'heartbeat\n')
                proc.stdin.flush()
                if action:
                    until = time.monotonic() + 5
                    while not (repo / 'started').exists() and proc.poll() is None and time.monotonic() < until:
                        time.sleep(0.02)
                    self.assertTrue((repo / 'started').exists())
                    action(proc)
                proc.wait(timeout=10)
                output = proc.stdout.read().decode() + proc.stderr.read().decode()
                metadata = json.loads((repo / 'results/test/_master/worker.json').read_text())
                cleaned = (repo / 'cleaned').exists()
                if (repo / 'started').exists():
                    pid = int((repo / 'started').read_text())
                    self.assertFalse(Path(f'/proc/{pid}').exists(), f'Child {pid} survived cancellation')
                return proc.returncode, metadata, cleaned, output
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if not stream.closed:
                        stream.close()

    code = ('import os,signal,time; from pathlib import Path; '
            'signal.signal(signal.SIGINT, lambda *a: (Path("cleaned").touch(), exit(0))); '
            'Path("started").write_text(str(os.getpid())); time.sleep(30)')

    def test_child_failure_propagates(self):
        code, meta, _, output = self.run_job('raise SystemExit(17)')
        self.assertEqual(code, 17, output)
        self.assertEqual(meta['status'], 'failed')

    def test_ssh_eof_cleans_owned_child_and_marks_failure(self):
        code, meta, cleaned, output = self.run_job(self.code, lambda p: p.stdin.close())
        self.assertNotEqual(code, 0, output)
        self.assertEqual(meta['error'], 'Master SSH input closed')
        self.assertTrue(cleaned)

    def test_heartbeat_expiry_cleans_even_when_connection_stays_open(self):
        code, meta, cleaned, output = self.run_job(self.code, lambda p: None, heartbeat_timeout=0.5)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(meta['error'], 'Master heartbeat expired')
        self.assertTrue(cleaned)

    def test_termination_runs_cleanup(self):
        code, meta, cleaned, output = self.run_job(self.code, lambda p: p.send_signal(signal.SIGTERM))
        self.assertNotEqual(code, 0, output)
        self.assertEqual(meta['error'], 'Interrupted')
        self.assertTrue(cleaned)


class SimulatedSSH(unittest.TestCase):
    def test_pair_success_and_receiver_failure_with_real_remote_supervisors(self):
        import socket
        for fail in (False, True):
            with self.subTest(receiver_failure=fail), tempfile.TemporaryDirectory(prefix='master-ssh-test-') as d:
                root = Path(d)
                bind = root / 'bin'
                bind.mkdir()
                ssh = bind / 'ssh'
                # This transport runs locally. Rewrite host identity only to model
                # two different machines; all supervisor processes/signals are real.
                ssh.write_text('#!/usr/bin/env python3\n'
                    'import shlex,subprocess,sys\n'
                    'p=subprocess.Popen(shlex.split(sys.argv[-1]),stdin=sys.stdin,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)\n'
                    'for raw in p.stdout:\n'
                    ' line=raw.decode()\n'
                    ' if line.startswith("MASTER_HOST "): line="MASTER_HOST simulated-"+sys.argv[-2]+"\\n"\n'
                    ' print(line,end="",flush=True)\n'
                    'raise SystemExit(p.wait())\n')
                ssh.chmod(0o755)
                config = configuration()
                jobs = {}
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
                for role in testbed.ROLES:
                    repo = root / role
                    (repo / 'common').mkdir(parents=True)
                    (repo / 'scripts').mkdir()
                    (repo / 'scripts/remote_job.py').symlink_to(ROOT / 'scripts/remote_job.py')
                    (repo / 'common/environment.sh').write_text('rp_exp_source_setup() { source "$1"; }\n')
                    (repo / 'setup.bash').write_text('true\n')
                    config['hosts'][role]['repo'] = str(repo)
                    config['hosts'][role]['ros_setup'] = str(repo / 'setup.bash')
                    code = ('import socket,signal; from pathlib import Path; '
                            'signal.signal(signal.SIGINT,lambda *a:(Path("cleaned").touch(),exit(0))); '
                            f's=socket.socket(); s.bind(("127.0.0.1",{port})); s.listen(1); '
                            'c,_=s.accept(); c.close(); s.close()') if role == 'slave_a' else (
                            'raise SystemExit(17)' if fail else
                            f'import socket; s=socket.create_connection(("127.0.0.1",{port}),3); s.close()')
                    jobs[role] = testbed.request(config, role, 'trial', role,
                        [sys.executable, '-u', '-c', code], timeout=10, workspace=False, reserve=True,
                        **({'ready_port': port, 'unused_ports': [port]} if role == 'slave_a' else {}))
                output = root / 'master'
                output.mkdir()
                with patch.dict(os.environ, {'PATH': str(bind) + os.pathsep + os.environ['PATH']}):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, 'Slave job failed'):
                            master.execute(config, jobs, output, paired=True)
                        self.assertTrue((root / 'slave_a/cleaned').exists())
                    else:
                        master.execute(config, jobs, output, paired=True)
                for role in testbed.ROLES:
                    meta = json.loads((root / role / 'results/trial/_master' / (role + '.json')).read_text())
                    self.assertEqual(meta['status'], 'failed' if fail else 'complete')


if __name__ == '__main__':
    unittest.main()
