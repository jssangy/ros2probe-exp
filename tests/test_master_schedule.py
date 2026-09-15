"""Master scheduling, clock guards and collection-before-analysis behavior."""
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import queue
import unittest
from unittest.mock import Mock, patch

from common import clock_sync, schedule
from common.sequence_data import read_sequences, write_sequences
from common.check_measurements import check
from test_experiment_evidence import timed_log
from scripts.report_results import prepare
from test_master_runner import ROOT, configuration, master, testbed


class ClockAndSchedule(unittest.TestCase):
    def test_readiness_wait_retries_transient_slew_without_clock_mutation(self):
        row = 'C0000201,192.0.2.1,3,1700000000.000,0.0001,0.0002,0.0003,1.0,0.1,0.2,0.004,0.005,1.0,Normal'
        calls = []
        def chrony(*args, **kwargs):
            calls.append(args)
            self.assertFalse(kwargs.get('privileged'))
            if len(calls) == 1:
                raise subprocess.CalledProcessError(1, 'chronyc waitsync')
            return row
        with patch.object(clock_sync, 'chronyc', chrony), patch.object(clock_sync.time, 'sleep'):
            self.assertEqual(clock_sync.wait_ready('192.0.2.1')['system_offset_s'], .0001)
        self.assertEqual(calls, [('waitsync', 1, .001), ('waitsync', 1, .001), ('-c', '-n', 'tracking')])

    def test_readiness_wait_cannot_accept_a_persistent_clock_failure(self):
        with patch.object(clock_sync, 'check', side_effect=RuntimeError('wrong source')), \
             patch.object(clock_sync.time, 'monotonic', side_effect=[0, 1, 16]), \
             patch.object(clock_sync.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'did not settle'):
                clock_sync.wait_ready('192.0.2.1')

    def test_master_keeps_heartbeats_and_assigns_epoch_only_after_both_clocks_pass(self):
        remote = master.Remote.__new__(master.Remote)
        remote.proc = Mock(); remote.proc.poll.return_value = None
        remote.events = queue.Queue()
        remote.events.put('MASTER_SCHEDULE ' + json.dumps(dict(id='trial', command='START S1')))
        remote.job = dict(master_schedule=True, schedule_clock_checks=True, schedule_lead_sec=5)
        remote.scheduled = set(); remote.schedule_request = None; remote.trial_clocks = None
        remote.clock_config = configuration(); remote.pending_input = b''; remote.last_heartbeat = 0
        remote.log = io.StringIO()
        clocks = dict(slave_a={'system_offset_s': .0001}, slave_b={'system_offset_s': .0002})
        with patch.object(master, 'TrialClocks') as checker, \
             patch.object(master.os, 'write', side_effect=lambda fd, data: len(data)) as write:
            checker.return_value.poll.side_effect = [None, clocks]
            remote.tick()
            self.assertNotIn('MASTER_ASSIGNED', remote.log.getvalue())
            self.assertEqual(write.call_args.args[1], b'heartbeat\n')
            before = time.time_ns()
            remote.tick()
        assigned = json.loads(remote.log.getvalue().split('MASTER_ASSIGNED ', 1)[1])
        self.assertGreaterEqual(assigned['target_ns'], before + 5_000_000_000)
        self.assertEqual(assigned['clocks'], clocks)
        checker.return_value.close.assert_called_once()

    def test_trial_clock_failure_and_timeout_do_not_authorize_a_schedule(self):
        check = master.TrialClocks.__new__(master.TrialClocks)
        check.results = {}; check.deadline = 0
        failed = Mock(); failed.poll.return_value = 1; failed.returncode = 1
        failed.communicate.return_value = ('', 'clock outside tolerance')
        check.processes = {'slave_a': failed}
        with self.assertRaisesRegex(RuntimeError, 'slave_a clock readiness failed'):
            check.poll()
        failed.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, '20s timeout'):
            check.poll()
        check.close()
        failed.terminate.assert_called_once()

    def test_link_change_discards_old_samples_before_requesting_fresh_master_time(self):
        old = 'C0000201,192.0.2.1,3,1700000000.000,0.0001,0.0002,0.0003,1.0,0.1,0.2,0.004,0.005,1.0,Normal'
        reset = old.replace('1700000000.000', '0.000').replace('Normal', 'Not synchronised')
        fresh = old.replace('1700000000.000', '1700000002.000')
        calls = []
        pending = iter([reset, reset, fresh, fresh])
        def chrony(*args, **kwargs):
            calls.append((args, kwargs))
            if args[-1] == 'sources': return '^,*,192.0.2.1,3,4,377,1,0,0,0'
            if args[-1] == 'activity': return '9,0,0,0,0'
            if args[-1] == 'tracking': return next(pending)
            return '200 OK'
        with patch.object(clock_sync, 'chronyc', chrony), patch.object(clock_sync.time, 'sleep'):
            result = clock_sync.synchronize('192.0.2.1', reset_sources=True)
        operations = [args for args, _ in calls]
        self.assertLess(operations.index(('reset', 'sources')), operations.index(('-c', '-n', 'tracking')))
        self.assertLess(operations.index(('online', '192.0.2.1')), operations.index(('burst', '4/8', '192.0.2.1')))
        self.assertEqual(sum(args[-1] == 'tracking' for args in operations[:operations.index(('makestep',))]), 3)
        self.assertTrue(result['source_measurements_reset'])
        self.assertIn(('waitsync', 1, .001), operations)
        with patch.object(clock_sync, 'chronyc') as command:
            with self.assertRaisesRegex(ValueError, 'only on a slave'):
                clock_sync.synchronize(reset_sources=True)
            command.assert_not_called()

    def test_stale_clock_after_link_reset_cannot_authorize_a_step(self):
        old = 'C0000201,192.0.2.1,3,1700000000.000,0.0001,0.0002,0.0003,1.0,0.1,0.2,0.004,0.005,1.0,Normal'
        with patch.object(clock_sync, 'chronyc', return_value=old) as command, \
             patch.object(clock_sync.time, 'monotonic', side_effect=[0, 1, 91]), \
             patch.object(clock_sync.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'No fresh synchronized'):
                clock_sync.synchronize('192.0.2.1', reset_sources=True)
        self.assertFalse(any(call.args[0] == 'makestep' for call in command.call_args_list))

    def test_first_good_sample_does_not_release_workers_during_remaining_burst(self):
        old = 'C0000201,192.0.2.1,3,1700000000.000,0.0001,0.0002,0.0003,1.0,0.1,0.2,0.004,0.005,1.0,Normal'
        fresh = old.replace('1700000000.000', '1700000001.000')
        tracking = iter([old, fresh, fresh, fresh])
        activity = iter(['8,0,1,0,0', '9,0,0,0,0'])
        operations = []
        def chrony(*args, **kwargs):
            operations.append(args)
            if args[-1] == 'sources': return '^,*,192.0.2.1,3,4,377,1,0,0,0'
            if args[-1] == 'tracking': return next(tracking)
            if args[-1] == 'activity': return next(activity)
            return '200 OK'
        with patch.object(clock_sync, 'chronyc', chrony), patch.object(clock_sync.time, 'sleep'):
            clock_sync.synchronize('192.0.2.1')
        before_step = operations[:operations.index(('makestep',))]
        self.assertEqual(before_step.count(('-c', 'activity')), 2)

    def test_master_and_read_only_check_never_reset_clock_samples(self):
        for action in ('run', 'sync-clocks', 'check'):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as d:
                args = master.parser().parse_args([action, '4', '--run-id', 'clock-flags'])
                addresses = json.dumps([dict(addr_info=[dict(local='192.0.2.1')])])
                with patch.object(master.subprocess, 'check_output', return_value=addresses), \
                     patch.object(master.subprocess, 'run') as local, \
                     patch.object(master, 'execute') as remote:
                    master.synchronize_clocks(configuration(), args, Path(d))
                self.assertNotIn('--reset-sources', local.call_args.args[0])
                jobs = remote.call_args.args[1]
                for job in jobs.values():
                    self.assertEqual('--reset-sources' in job['argv'], action != 'check')
                    self.assertEqual('--check-only' in job['argv'], action == 'check')

    def test_clock_step_requires_a_fresh_sample_and_never_arms_a_future_step(self):
        old = 'C0000201,192.0.2.1,3,1700000000.000,0.0001,0.0002,0.0003,1.0,0.1,0.2,0.004,0.005,1.0,Normal'
        new = old.replace('1700000000.000', '1700000001.000')
        calls=[]; tracking_count=0
        def chrony(*args, **kwargs):
            nonlocal tracking_count
            calls.append((args,kwargs))
            if args[-1] == 'sources': return '^,*,192.0.2.1,3,4,377,1,0,0,0'
            if args[-1] == 'activity': return '9,0,0,0,0'
            if args[-1] == 'tracking':
                tracking_count += 1
                return old if tracking_count <= 2 else new
            return '200 OK'
        with patch.object(clock_sync,'chronyc',chrony), patch.object(clock_sync.time,'sleep'):
            clock_sync.synchronize('192.0.2.1')
        step=[i for i,(args,kwargs) in enumerate(calls) if args[0]=='makestep']
        self.assertEqual(len(step),1)
        self.assertEqual(calls[step[0]][0],('makestep',))
        self.assertEqual(sum(args[-1]=='tracking' for args,_ in calls[:step[0]]),3)

    def test_tracking_csv_field_positions_and_required_master(self):
        # chronyc's CSV includes both reference ID and source name before stratum.
        row = 'C0000201,192.0.2.1,3,1700000000.000,0.0001,0.0002,0.0003,1.0,0.1,0.2,0.004,0.005,1.0,Normal'
        with patch.object(clock_sync, 'chronyc', return_value=row):
            value = clock_sync.check('192.0.2.1')
            self.assertEqual(value['system_offset_s'], .0001)
            self.assertEqual(value['root_delay_s'], .004)
            self.assertEqual(value['root_dispersion_s'], .005)
            with self.assertRaisesRegex(RuntimeError, 'must select master'):
                clock_sync.check('192.0.2.2')
        with patch.object(clock_sync, 'chronyc', return_value=row.replace('Normal', 'Not synchronised')):
            with self.assertRaises(RuntimeError): clock_sync.tracking()

    def test_wait_is_not_early_and_past_target_is_rejected(self):
        target = time.time_ns() + 100_000_000
        actual = schedule.wait_until(target, tolerance_ms=1000, check_clock=False)
        self.assertGreaterEqual(actual, target)
        with self.assertRaisesRegex(RuntimeError, 'missed'):
            schedule.wait_until(time.time_ns() - 1, check_clock=False)

    def test_late_dispatch_is_logged_and_fails(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(schedule.time, 'time_ns', side_effect=[1_000_000_000, 1_400_000_000]), \
             patch.object(schedule.time, 'monotonic_ns', side_effect=[1_000_000_000, 1_400_000_000]):
            log=Path(d)/'timing.jsonl'
            with self.assertRaisesRegex(RuntimeError, 'tolerance'):
                schedule.wait_until(1_100_000_000, log=log, check_clock=False)
            self.assertEqual(json.loads(log.read_text())['lateness_ms'], 300)

    def test_real_supervisor_delivers_master_schedule_over_control_channel(self):
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d); (repo/'common').mkdir(); (repo/'scripts').mkdir()
            (repo/'common/environment.sh').write_text('rp_exp_source_setup() { source "$1"; }\n')
            (repo/'setup.bash').write_text('true\n')
            (repo/'scripts/remote_job.py').symlink_to(ROOT/'scripts/remote_job.py')
            bind=repo/'bin'; bind.mkdir()
            ssh=bind/'ssh'
            ssh.write_text('#!/usr/bin/env python3\nimport shlex,subprocess,sys\n'
                           'p=subprocess.Popen(shlex.split(sys.argv[-1]),stdin=sys.stdin,stdout=subprocess.PIPE)\n'
                           'for line in p.stdout:\n'
                           ' print("MASTER_HOST simulated-slave" if line.startswith(b"MASTER_HOST ") else line.decode().rstrip(),flush=True)\n'
                           'raise SystemExit(p.wait())\n')
            ssh.chmod(0o755)
            config=configuration(); config['hosts']['slave_b']['repo']=d
            config['hosts']['slave_b']['ros_setup']=str(repo/'setup.bash')
            code = f'''import sys,os,json,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,{str(ROOT)!r})
from common.schedule import request_time,wait_until
with patch('common.schedule.check_environment',return_value={{}}):
 value=request_time(os.environ['RP_EXP_SCHEDULE_DIR'],'START S1')
 actual=wait_until(value['target_ns'],check_clock=False,tolerance_ms=1000)
Path('observed.json').write_text(json.dumps(dict(value,actual_ns=actual)))
'''
            job=testbed.request(config,'slave_b','schedule-test','receiver',
                                [sys.executable,'-c',code],workspace=False,reserve=True,
                                timeout=10,master_schedule=True,schedule_lead_sec=1)
            with patch.dict(os.environ, PATH=str(bind)+os.pathsep+os.environ['PATH']):
                master.execute(config,{'slave_b':job},repo)
            observed=json.loads((repo/'observed.json').read_text())
            self.assertGreaterEqual(observed['actual_ns'],observed['target_ns'])
            self.assertIn('MASTER_ASSIGNED', (repo/'receiver-slave_b.ssh.log').read_text())


class LocalAnalysis(unittest.TestCase):
    def test_scheduled_run_rejects_data_arriving_before_target(self):
        with tempfile.TemporaryDirectory() as d:
            run=Path(d)
            (run/'execution_profile.txt').write_text('master-scheduled-v1\n')
            (run/'timing.jsonl').write_text(json.dumps(dict(event='armed',target_ns=2_000_000_000))+'\n'+json.dumps(dict(event='publisher_ready',reply_ns=2_010_000_000))+'\n')
            (run/'platform.log').write_text('scenario=S1\ncondition=baseline\n')
            (run/'sub.log').write_text(timed_log())
            (run/'netdev.log').write_text('0 0\n1000 100\n2000 200\n3000 300\n4000 400\n')
            with self.assertRaisesRegex(ValueError,'before the scheduled start'): check(run)

    def test_scheduled_and_direct_runs_are_not_pooled(self):
        rows=[dict(scenario='S1',condition='baseline',run=f'run{i}',rx_mbps='1',drop_pct='0',execution_profile=profile)
              for i,profile in enumerate(['direct','master-scheduled-v1'])]
        with self.assertRaisesRegex(ValueError,'execution profiles'): prepare(4,rows)

    def test_measure_export_collect_then_local_analysis_and_no_analysis_after_failed_collect(self):
        for fail_collect in (False, True):
            with self.subTest(fail_collect=fail_collect), tempfile.TemporaryDirectory() as d:
                root=Path(d); config=root/'config.json';config.write_text(json.dumps(configuration()))
                events=[]
                def execute(config, jobs, output, paired=False):
                    job=next(iter(jobs.values()))
                    events.append('export' if job['job_id'].startswith('export') else 'measure' if paired else 'preflight')
                def collect(*args, **kwargs):
                    events.append('collect')
                    if fail_collect: raise RuntimeError('rsync failed')
                class Network:
                    def __init__(self,*args): pass
                    def start(self): events.append('isolate')
                    def check(self): pass
                    def close(self): events.append('restore')
                with patch.object(master,'ROOT',root), patch.object(master,'execute',execute), \
                     patch.object(master,'NetworkSession',Network), \
                     patch.object(master,'synchronize_clocks',lambda *a,**k:events.append('clock')), \
                     patch.object(master,'collect',collect), \
                     patch.object(master,'analyze_collected',lambda *a:events.append('analyze')), \
                     patch.object(sys,'argv',['run_experiments.py','--config',str(config),'run','3','--run-id','trial']):
                    self.assertEqual(master.main(), int(fail_collect))
                expected=['isolate','preflight','clock','measure','export','restore','collect']+([] if fail_collect else ['analyze'])
                self.assertEqual(events,expected)

    def test_portable_sequence_data_needs_no_ros_and_checks_hash(self):
        with tempfile.TemporaryDirectory() as d:
            write_sequences(d,{1,3},3,{'bag':'slave-local.mcap'})
            self.assertEqual(read_sequences(d,3),{1,3})
            (Path(d)/'observer_sequences.csv').write_text('sequence\n1\n2\n')
            with self.assertRaisesRegex(ValueError,'hash'):
                read_sequences(d,3)


if __name__ == '__main__': unittest.main()
