#!/usr/bin/env python3
"""Control all four experiments over SSH from one master and two slave laptops."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time

from testbed import (ROOT, ROLES, PAPER_NP, RESOURCE_SCENARIOS, PROBE_SCENARIOS, LOSS_LEVELS,
                     analysis_jobs, default_config, export_jobs, label, load, remote_path,
                     request, stages, time_budget)
from ssh_access import SSH_OPTIONS, prepare_access, ssh_options
from export_results import export_results, result_paths

NETWORK_SESSION = None


class TrialClocks:
    """Inspect both slaves without blocking controller heartbeats or stepping time."""
    def __init__(self, config):
        self.processes, self.results = {}, {}
        self.deadline = time.monotonic() + 20
        try:
            for role in ROLES:
                host = config['hosts'][role]
                command = ('python3 ' + remote_path(host['repo'].rstrip('/') + '/common/clock_sync.py') +
                           ' --check-only --wait-seconds 15 --source ' + shlex.quote(config['master_clock_address']))
                self.processes[role] = subprocess.Popen(
                    ['ssh', '-o', 'ConnectTimeout=3', *ssh_options(),
                     '-o', 'StrictHostKeyChecking=yes', host['ssh'], command],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except BaseException:
            self.close()
            raise

    def poll(self):
        for role, proc in self.processes.items():
            if role in self.results or proc.poll() is None:
                continue
            out, err = proc.communicate()
            if proc.returncode:
                raise RuntimeError(f'{role} clock readiness failed: {err.strip()}')
            self.results[role] = json.loads(out)
        if len(self.results) == len(ROLES):
            return self.results
        if time.monotonic() > self.deadline:
            raise RuntimeError('Both slave clocks must pass before assigning the start time (20s timeout)')
        return None

    def close(self):
        for proc in self.processes.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in self.processes.values():
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


class Remote:
    def __init__(self, host, job, output):
        self.role = host
        self.job = job
        self.ready = False
        self.identity = None
        self.events = queue.Queue()
        self.last_heartbeat = 0
        self.pending_input = b''
        self.scheduled = set()
        self.clock_config, self.trial_clocks, self.schedule_request = None, None, None
        self.network_states = []
        encoded = base64.b64encode(json.dumps(job).encode()).decode()
        worker = 'network_guard.py' if job.get('network_guard') else 'remote_job.py'
        command = 'python3 ' + remote_path(job['repo'].rstrip('/') + '/scripts/' + worker) + ' ' + encoded
        self.log = output.open('w')
        try:
            self.proc = subprocess.Popen(['ssh', *ssh_options(), host, command], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        except OSError:
            self.log.close()
            raise
        os.set_blocking(self.proc.stdin.fileno(), False)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for raw in self.proc.stdout:
            line = raw.decode(errors='replace').rstrip()
            self.log.write(line + '\n')
            self.log.flush()
            self.events.put(line)

    def tick(self):
        code = self.proc.poll()
        if code is not None:
            # Drain the final identity/status lines before reporting completion.
            self.reader.join(timeout=2)
        while True:
            try:
                line = self.events.get_nowait()
            except queue.Empty:
                break
            if line == 'MASTER_READY':
                self.ready = True
            elif line.startswith('MASTER_HOST '):
                self.identity = line.split(' ', 1)[1]
            elif line.startswith('NETWORK_STATE '):
                self.network_states.append(json.loads(line.split(' ', 1)[1]))
            elif line.startswith('MASTER_SCHEDULE '):
                value = json.loads(line.split(' ', 1)[1])
                if not self.job.get('master_schedule') or value['id'] in self.scheduled:
                    raise RuntimeError('Unexpected or duplicate slave schedule request')
                self.scheduled.add(value['id'])
                if self.schedule_request is not None:
                    raise RuntimeError('Previous schedule readiness is still pending')
                self.schedule_request = value
                if self.job.get('schedule_clock_checks'):
                    if self.clock_config is None:
                        raise RuntimeError('Missing clock configuration for master schedule barrier')
                    self.trial_clocks = TrialClocks(self.clock_config)
            else:
                print(f'[{self.role}] {line}', flush=True)
        if self.schedule_request is not None and code is None:
            clocks = self.trial_clocks.poll() if self.trial_clocks is not None else {}
            if clocks is not None:
                if self.trial_clocks is not None:
                    self.trial_clocks.close()
                    self.trial_clocks = None
                value = self.schedule_request
                reply = dict(id=value['id'], target_ns=time.time_ns() +
                             int(self.job.get('schedule_lead_sec', 5) * 1e9), clocks=clocks)
                self.pending_input += (json.dumps(reply) + '\n').encode()
                self.log.write('MASTER_ASSIGNED ' + json.dumps(reply) + '\n')
                self.log.flush()
                self.schedule_request = None
                print(f'[master] {value["command"]}: scheduled epoch_ns={reply["target_ns"]}', flush=True)
        if self.proc.poll() is None and time.monotonic() - self.last_heartbeat >= 5:
            self.pending_input += b'heartbeat\n'
            self.last_heartbeat = time.monotonic()
        if self.proc.poll() is None and self.pending_input:
            try:
                sent = os.write(self.proc.stdin.fileno(), self.pending_input)
                self.pending_input = self.pending_input[sent:]
            except (BrokenPipeError, BlockingIOError):
                pass
        return code

    def close_input(self):
        if self.trial_clocks is not None:
            self.trial_clocks.close()
            self.trial_clocks = None
        self.schedule_request = None
        if not self.proc.stdin.closed:
            self.proc.stdin.close()

    def finish(self):
        self.close_input()
        self.reader.join(timeout=2)
        self.tick()
        self.proc.stdout.close()
        self.log.close()


def stop_all(jobs):
    for job in jobs:
        job.close_input()
    deadline = time.monotonic() + 65
    while any(job.proc.poll() is None for job in jobs) and time.monotonic() < deadline:
        time.sleep(0.2)
    for job in jobs:
        if job.proc.poll() is None:
            job.proc.terminate()
            try:
                job.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                job.proc.kill()
                job.proc.wait()
        job.finish()


def wait_for(jobs, predicate, timeout):
    deadline = time.monotonic() + timeout
    next_update = time.monotonic() + 30
    while True:
        if NETWORK_SESSION is not None:
            NETWORK_SESSION.check()
        codes = [job.tick() for job in jobs]
        if any(code not in (None, 0) for code in codes):
            raise RuntimeError('Slave job failed; see the saved master/slave logs')
        if predicate(codes):
            return
        if time.monotonic() > deadline:
            raise RuntimeError('Timed out waiting for slave jobs')
        if time.monotonic() >= next_update:
            print('[master] Waiting for ' + ', '.join(job.job['job_id'] for job in jobs if job.proc.poll() is None), flush=True)
            next_update = time.monotonic() + 30
        time.sleep(0.2)


class NetworkSession:
    """Keep each slave guard alive across preflight, clock sync and all conditions."""
    def __init__(self, config, run_id, output):
        self.config, self.run_id, self.output = config, run_id, output
        self.jobs, self.error = [], None
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        master_id = Path('/etc/machine-id').read_text().strip()
        for role in ROLES:
            host = self.config['hosts'][role]
            job = dict(host, job_id='network-isolation', network_guard=True,
                       run_id=self.run_id, link=self.config['link'], master_machine_id=master_id,
                       require_ssh_confirmation=True)
            print(f'[master] {role}: keep {host["nic"]}; disable opposite physical link', flush=True)
            self.jobs.append(Remote(host['ssh'], job, self.output / f'network-{role}.ssh.log'))
        wait_for(self.jobs, lambda codes: all(j.ready for j in self.jobs) or any(c is not None for c in codes), 120)
        if not all(j.ready and j.proc.poll() is None for j in self.jobs):
            raise RuntimeError('Network isolation failed before readiness')
        if len({j.identity for j in self.jobs}) != 2 or master_id in {j.identity for j in self.jobs}:
            raise RuntimeError('Network guards must run on two distinct slaves')
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()
        self.confirm_ssh()

    def confirm_ssh(self, timeout=20):
        """Require two rounds of fresh SSH, then stop startup ARP refreshes."""
        report = dict(status='incomplete', attempts=[])
        deadline, successes = time.monotonic() + timeout, 0

        def probe(role):
            host = self.config['hosts'][role]
            try:
                result = subprocess.run(['ssh', '-o', 'ConnectTimeout=2', *ssh_options(),
                    '-o', 'StrictHostKeyChecking=yes', host['ssh'], 'true'],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=4)
                return dict(ssh=host['ssh'], returncode=result.returncode)
            except subprocess.TimeoutExpired:
                return dict(ssh=host['ssh'], returncode=None)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                while time.monotonic() < deadline:
                    self.check()
                    results = dict(zip(ROLES, pool.map(probe, ROLES)))
                    report['attempts'].append(dict(time=time.time(), hosts=results))
                    successes = successes + 1 if all(r['returncode'] == 0 for r in results.values()) else 0
                    if successes >= 2:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('Selected-link SSH did not stabilize after interface isolation; measurement not started')
            for job in self.jobs:
                job.pending_input += b'confirm-ssh\n'
            # The guard acknowledges that setup-only ARP refreshes have stopped.
            deadline = time.monotonic() + 5
            while not all(job.network_states and job.network_states[-1].get('ssh_confirmed_at') for job in self.jobs):
                self.check()
                if time.monotonic() >= deadline:
                    raise RuntimeError('Network guards did not acknowledge SSH confirmation')
                time.sleep(.1)
            report['status'] = 'complete'
            print('[master] Selected-link SSH confirmed on both slaves; startup ARP refreshes stopped', flush=True)
        except BaseException as exc:
            report.update(status='failed', error=str(exc))
            raise
        finally:
            (self.output / 'network-ssh.json').write_text(json.dumps(report, indent=2) + '\n')

    def pump(self):
        try:
            while not self.stopping.is_set():
                for job in self.jobs:
                    if job.tick() is not None:
                        raise RuntimeError('Slave network isolation ended unexpectedly')
                self.stopping.wait(0.2)
        except Exception as exc:
            self.error = str(exc)

    def check(self):
        if self.error:
            raise RuntimeError(self.error)

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=3)
        for job in self.jobs:
            if job.proc.poll() is None:
                job.pending_input += b'restore\n'
                job.tick()
        stop_all(self.jobs)
        errors = []
        for role, job in zip(ROLES, self.jobs):
            (self.output / f'network-{role}.json').write_text(json.dumps(job.network_states, indent=2) + '\n')
            if job.proc.returncode or not job.network_states or job.network_states[-1]['status'] != 'restored':
                errors.append(role + ': isolation or restoration failed; inspect network logs')
        if errors:
            raise RuntimeError('; '.join(errors))


def execute(config, requests, output, paired=False):
    if NETWORK_SESSION is not None:
        NETWORK_SESSION.check()
    jobs = []
    try:
        for role, job in requests.items():
            print(f'[master] {role}: {job["job_id"]}', flush=True)
            remote = Remote(config['hosts'][role]['ssh'], job, output / (job['job_id'] + '-' + role + '.ssh.log'))
            if job.get('schedule_clock_checks'):
                remote.clock_config = config
            jobs.append(remote)
            if paired and role == 'slave_a':
                wait_for(jobs, lambda codes: remote.ready or codes[0] is not None, 120)
                if not remote.ready or remote.proc.poll() is not None:
                    raise RuntimeError('Publisher exited before its command listener was ready')
        # DONE can let the publisher exit shortly before the receiver; allow that,
        # but do not leave the surviving side running for a whole batch timeout.
        completed_at = None

        def finished(codes):
            nonlocal completed_at
            if all(code is not None for code in codes):
                return True
            if paired and any(code is not None for code in codes):
                completed_at = completed_at or time.monotonic()
                if time.monotonic() - completed_at > 30:
                    raise RuntimeError('Only one slave finished; stopping the remaining controller')
            return False

        wait_for(jobs, finished, max(job['timeout'] for job in requests.values()) + 70)
        identities = [job.identity for job in jobs]
        if len(jobs) == 2 and (not all(identities) or identities[0] == identities[1]):
            raise RuntimeError('SSH targets must resolve to two different slave machines')
        if Path('/etc/machine-id').exists() and Path('/etc/machine-id').read_text().strip() in identities:
            raise RuntimeError('The master must be a separate machine from both slaves')
    finally:
        stop_all(jobs)


def preflight(config, args, reserve=True):
    jobs = {}
    for role in ROLES:
        host = config['hosts'][role]
        peer = config['hosts'][ROLES[1] if role == ROLES[0] else ROLES[0]]
        argv = ['python3', 'scripts/check_testbed.py', '--role', role, '--experiment', args.experiment,
                '--address', host['address'], '--peer', peer['address'], '--nic', host['nic']]
        jobs[role] = request(config, role, args.run_id, 'preflight', argv, reserve=reserve)
    return jobs


def collect(config, run_id, destination, bags=False):
    errors = []
    for role in ROLES:
        host = config['hosts'][role]
        target = destination / role
        target.mkdir(parents=True, exist_ok=True)
        # --protect-args keeps remote paths with spaces out of the remote shell.
        # Trailing / copies contents; no --delete, no host merging, no moves.
        for tree in (('results', 'bags') if bags else ('results',)):
            folder = target if tree == 'results' else target / 'bags'
            folder.mkdir(parents=True, exist_ok=True)
            source = host['repo'].rstrip('/') + f'/{tree}/{run_id}/'
            if source.startswith('~/'):
                source = source[2:]  # rsync remote relative paths start at the SSH user's home.
            cmd = ['rsync', '-rt', '--protect-args', '-e', shlex.join(['ssh', *ssh_options()]),
                   '--', host['ssh'] + ':' + source, str(folder) + '/']
            result = subprocess.run(cmd, timeout=3600)
            if result.returncode:
                errors.append(f'{role}/{tree}: rsync exit {result.returncode}')
    if errors:
        raise RuntimeError('; '.join(errors))


def deploy(config, config_path=None):
    """Copy this working source tree; preserve each slave's builds and raw data."""
    excluded = ['.git', '.agents', '.codex', '.claude', '.obsidian', '.tools', '.testbed.json', '.testbed.*.json',
                '.artifact-env.sh', 'build', 'install', 'log', 'results', 'bags',
                'organized_results', 'reports', 'validation', '__pycache__']
    if config_path:
        try:
            excluded.append(Path(config_path).resolve().relative_to(ROOT).as_posix())
        except ValueError:
            pass
    for role in ROLES:
        host = config['hosts'][role]
        subprocess.run(['ssh', *ssh_options(), host['ssh'],
                        'mkdir -p -- ' + remote_path(host['repo'])], check=True, timeout=30)
        destination = host['repo'].rstrip('/') + '/'
        if destination.startswith('~/'):
            destination = destination[2:]
        argv = ['rsync', '-rltp', '--protect-args', '-e', shlex.join(['ssh', *ssh_options()])]
        for name in excluded:
            argv += ['--exclude', '/' + name if name != '__pycache__' else name]
        argv += ['--', str(ROOT) + '/', host['ssh'] + ':' + destination]
        print(f'[master] Deploying source to {role}: {host["ssh"]}:{host["repo"]}', flush=True)
        subprocess.run(argv, check=True, timeout=1800)


def synchronize_clocks(config, args, output, reserve=False):
    address = config.get('master_clock_address')
    if not address:
        raise ValueError('Set master_clock_address with scripts/configure_testbed.py first')
    addresses = json.loads(subprocess.check_output(['ip', '-j', 'address'], text=True))
    if address not in [entry.get('local') for row in addresses for entry in row.get('addr_info', [])]:
        raise ValueError('master_clock_address is not assigned to this master')
    read_only = args.action == 'check'
    flags = ['--check-only'] if read_only else ['--allow', *[config['hosts'][r]['address'] for r in ROLES]]
    with (output / 'clock-master.log').open('w') as log:
        subprocess.run([sys.executable, str(ROOT / 'common/clock_sync.py'), *flags,
                        '--output', str(output / 'clock-master.json')], check=True,
                       stdout=log, stderr=subprocess.STDOUT, timeout=120)
    jobs = {}
    for role in ROLES:
        # NetworkSession has just changed the path; old wired delay statistics
        # can reject every valid Wi-Fi sample (chrony maxdelaydevratio test).
        flags = ['--check-only'] if read_only else ['--reset-sources']
        jobs[role] = request(config, role, args.run_id, 'clock-sync',
            ['python3', 'common/clock_sync.py', '--source', address, *flags,
             '--output', f'results/{args.run_id}/_master/clock.json'],
            timeout=120, workspace=False, reserve=reserve)
    execute(config, jobs, output)


def analyze_collected(config, args, destination):
    output = destination / '_master'
    output.mkdir(parents=True, exist_ok=True)
    jobs = analysis_jobs(config, args, destination)
    for job in jobs:
        print(f'[master] Local analysis: {job["job_id"]}', flush=True)
        with (output / (job['job_id'] + '.log')).open('w') as log:
            subprocess.run([sys.executable, *job['argv']], cwd=ROOT,
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    export_results(config, args, destination, jobs)


def csv_choices(value, allowed, numeric=False):
    values = value.split(',')
    if not values or len(values) != len(set(values)) or any(item not in allowed for item in values):
        raise argparse.ArgumentTypeError('Choose distinct values from ' + ','.join(allowed))
    return list(map(int, values)) if numeric else values


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--config', type=Path, default=default_config())
    cli.add_argument('--link', choices=['wired', 'wireless'], help='select the link from the single-file config')
    commands = cli.add_subparsers(dest='action', required=True)
    for name in ('deploy', 'build', 'check', 'sync-clocks', 'run', 'collect', 'analyze'):
        command = commands.add_parser(name)
        command.add_argument('--dry-run', action='store_true', help='print a plan; no SSH or writes')
        if name == 'deploy':
            continue
        if name in ('collect', 'analyze'):
            command.add_argument('--run-id', required=True)
            if name == 'collect': command.add_argument('--bags', action='store_true')
            continue
        command.add_argument('experiment', nargs='?', default='all', choices=['all', '1', '2', '3', '4'])
        command.add_argument('--run-id', required=name == 'run')
        if name != 'run':
            continue
        command.add_argument('--prepare-ssh', action='store_true',
                             help='verify/register SSH keys for the selected link before any experiment activity')
        command.add_argument('--runs', type=int, default=10,
                             help='repetitions per condition (default: 10)')
        command.add_argument('--np', type=lambda s: csv_choices(s, list(map(str, PAPER_NP)), True), default=PAPER_NP)
        command.add_argument('--resource-scenarios', type=lambda s: csv_choices(s, RESOURCE_SCENARIOS), default=RESOURCE_SCENARIOS)
        command.add_argument('--probe-scenarios', type=lambda s: csv_choices(s, PROBE_SCENARIOS), default=PROBE_SCENARIOS)
        command.add_argument('--losses', type=lambda s: csv_choices(s, list(map(str, LOSS_LEVELS)), True), default=LOSS_LEVELS)
        command.add_argument('--qos', choices=['both', 'best_effort', 'reliable'], default='both')
        command.add_argument('--collect-bags', action='store_true', help='also copy potentially large bags to master')
    return cli


def run_main(cli, args):
    global NETWORK_SESSION
    try:
        config = {} if args.action == 'analyze' else load(args.config, args.link)
        if args.action == 'deploy':
            if args.dry_run:
                print(json.dumps(dict(source=str(ROOT), hosts=config['hosts'],
                                      action='copy source; preserve builds, local settings, results and bags'), indent=2))
            else:
                deploy(config, args.config)
            return 0
        args.run_id = label(args.run_id or args.action + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        config['_network_guard'] = args.action in ('run', 'check', 'sync-clocks')
        if args.action == 'run' and args.runs < 1:
            raise ValueError('--runs must be positive')
        if args.action in ('run', 'check', 'sync-clocks') and not config.get('master_clock_address'):
            raise ValueError('Set master_clock_address using scripts/configure_testbed.py')
    except (ValueError, KeyError, OSError) as exc:
        cli.error(str(exc))
    if args.action == 'run':
        plan = stages(config, args)
        analysis = analysis_jobs(config, args)
        initial = preflight(config, args)
    elif args.action == 'build':
        plan, analysis = [], []
        initial = {role: request(config, role, args.run_id, 'build',
                   ['bash', 'scripts/build.sh', args.experiment], timeout=7200,
                   workspace=False, reserve=True) for role in ROLES}
    elif args.action == 'check':
        plan, analysis, initial = [], [], preflight(config, args)
    else:
        plan, analysis, initial = [], [], {}
    destination = ROOT / 'results' / args.run_id
    if args.action == 'analyze':
        saved = json.loads((destination / '_master/run.json').read_text())
        config = saved['config']
        args.experiment = saved['arguments']['experiment']
        args.qos = saved['arguments'].get('qos', 'best_effort')
        for key, default in [('runs', 10), ('np', PAPER_NP), ('resource_scenarios', RESOURCE_SCENARIOS),
                             ('losses', LOSS_LEVELS), ('probe_scenarios', PROBE_SCENARIOS)]:
            setattr(args, key, saved['arguments'].get(key, default))
        if any(saved.get(key) != 'complete' for key in ('measurement_status', 'export_status', 'collection_status')):
            raise ValueError('Measurement, data export or collection is incomplete; inspect the run evidence')
        if args.dry_run:
            print(json.dumps(analysis_jobs(config, args, destination), indent=2))
        else:
            try:
                analyze_collected(config, args, destination)
            except (Exception, KeyboardInterrupt) as exc:
                saved.update(analysis_status='failed', status='failed', error=str(exc))
                (destination / '_master/run.json').write_text(json.dumps(saved, indent=2) + '\n')
                raise
            saved.update(analysis_status='complete', analyzed_at=time.time())
            saved['result_csvs'] = {exp: str(path) for exp, path in result_paths(config, args, destination).items()}
            if saved.get('network_restoration_status') != 'failed':
                saved.update(status='complete', error=None)
            (destination / '_master/run.json').write_text(json.dumps(saved, indent=2) + '\n')
            for path in saved['result_csvs'].values():
                print(f'[master] Results CSV: {path}')
        return 0
    if args.dry_run:
        print(json.dumps(dict(topology='master + slave_a + slave_b', config=config,
                              preflight_or_build=initial, stages=plan, analysis=analysis,
                              exports=export_jobs(config, args) if args.action == 'run' else [],
                              time_budget=time_budget(args) if args.action == 'run' else None,
                              result_csvs={exp: str(path) for exp, path in result_paths(config, args, destination).items()}
                                          if args.action == 'run' else {},
                              collect_to=str(destination),
                              prepare_ssh=getattr(args, 'prepare_ssh', False),
                              ssh_connections='close each command on completion; no persistent SSH multiplex sessions',
                              network_isolation='SSH must use selected data IP; opposite physical NICs down until workers finish; restore on exit',
                              order=('verify/register selected-link SSH -> ' if getattr(args, 'prepare_ssh', False) else '') +
                                    'isolate interfaces -> preflight -> clock sync -> slave measurement -> sequence export -> restore interfaces -> collect -> MASTER analysis -> close connections',
                              schedule='Discovery: epoch + 30s; Exp2/4 START and Exp3 GO: epoch + 5s after B is ready'), indent=2))
        return 0
    if args.action == 'collect':
        collect(config, args.run_id, destination, args.bags)
        manifest = destination / '_master/run.json'
        if manifest.exists():
            saved = json.loads(manifest.read_text())
            saved.update(collection_status='complete', collected_at=time.time())
            manifest.write_text(json.dumps(saved, indent=2) + '\n')
        print(f'[master] Collected results: {destination}')
        return 0
    destination.mkdir(parents=True, exist_ok=False)
    output = destination / '_master'
    output.mkdir()
    metadata = dict(config=config, arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                    status='incomplete', stages_completed=[], started=time.time())
    if args.action == 'run':
        metadata['time_budget'] = time_budget(args)
        seconds = metadata['time_budget']['fixed_seconds']
        print(f'[master] Fixed waits: approximately {seconds / 3600:.2f} hours; '
              'startup, clock checks, recording validation and collection add time.', flush=True)
    metadata_path = output / 'run.json'
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f'Signal {signum}')

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    failure = None
    network = None
    ssh_ready = not getattr(args, 'prepare_ssh', False)
    try:
        if not ssh_ready:
            metadata['ssh_status'] = 'incomplete'
            metadata['ssh_hosts'] = prepare_access(config, args.config)
            ssh_ready = True
            metadata['ssh_status'] = 'complete'
            metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
        network = NetworkSession(config, args.run_id, output) if config.get('_network_guard') else None
        if network:
            network.start()
            NETWORK_SESSION = network
        if initial:
            execute(config, initial, output)
        if args.action in ('run', 'check', 'sync-clocks'):
            synchronize_clocks(config, args, output, reserve=args.action == 'sync-clocks')
        for stage in plan:
            if not stage['paired']:
                target = int(time.time()) + 30
                for job in stage['jobs'].values():
                    pos = job['argv'].index('--target-time')
                    job['argv'][pos + 1] = str(target)
            execute(config, stage['jobs'], output, stage['paired'])
            metadata['stages_completed'].append(stage['name'])
            metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
            if stage['cooldown']:
                print(f'[master] Discovery cooldown: {stage["cooldown"]}s', flush=True)
                time.sleep(stage['cooldown'])
        metadata['measurement_status'] = 'complete'
        for role, job in (export_jobs(config, args) if args.action == 'run' else []):
            execute(config, {role: job}, output)
        metadata['export_status'] = 'complete'
    except (Exception, KeyboardInterrupt) as exc:
        if not ssh_ready:
            metadata['ssh_status'] = 'failed'
        failure = str(exc) or type(exc).__name__
        print(f'[ERROR] {failure}', file=sys.stderr)
    finally:
        NETWORK_SESSION = None
        if network:
            try:
                network.close()
                metadata['network_restoration_status'] = 'complete'
            except (Exception, KeyboardInterrupt) as exc:
                metadata['network_restoration_status'] = 'failed'
                failure = (failure + '; ' if failure else '') + f'Network cleanup failed: {exc}'
        # Workers have finished or received cancellation before copying any data.
        if ssh_ready:
            try:
                collect(config, args.run_id, destination, getattr(args, 'collect_bags', False))
                metadata['collection_status'] = 'complete'
            except (Exception, KeyboardInterrupt) as exc:
                metadata['collection_status'] = 'failed'
                failure = (failure + '; ' if failure else '') + f'Collection failed: {exc}'
        else:
            metadata['collection_status'] = 'not-started'
        if not failure and args.action == 'run':
            try:
                analyze_collected(config, args, destination)
                metadata['analysis_status'] = 'complete'
                metadata['result_csvs'] = {exp: str(path) for exp, path in result_paths(config, args, destination).items()}
            except (Exception, KeyboardInterrupt) as exc:
                failure = f'Master analysis failed: {exc}'
        metadata.update(status='failed' if failure else 'complete', error=failure, ended=time.time())
        metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f'[master] {metadata["status"]}: {destination}')
    if not failure and args.action == 'run':
        for path in metadata['result_csvs'].values():
            print(f'[master] Results CSV: {path}')
        print(f'[master] Elapsed: {(metadata["ended"] - metadata["started"]) / 3600:.2f} hours')
    return 1 if failure else 0


def main():
    cli = parser()
    args = cli.parse_args()
    if args.dry_run:
        return run_main(cli, args)
    # Prevent another invocation from stepping clocks or copying data during this
    # master's active run. The context releases the lock on every return/error.
    lock_path = ROOT / 'results/.orchestrator.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as master_lock:
        try:
            fcntl.flock(master_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            cli.error('Another master operation is active; wait before changing clocks or collecting data')
        return run_main(cli, args)


if __name__ == '__main__':
    raise SystemExit(main())
