#!/usr/bin/env python3
"""Short, real two-slave S1 exchanges; no sudo, clock changes, rp or paper analysis."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from testbed import ROOT, ROLES, default_config, label, load, request
from run_experiments import Remote, collect, stop_all, wait_for


def counters():
    values = {}
    for path in Path('/sys/class/net').iterdir():
        try:
            values[path.name] = {key: int((path / 'statistics' / key).read_text())
                                 for key in ('rx_bytes', 'tx_bytes')}
        except OSError:
            pass
    return values


def peer(args):
    nic, address, other = (os.environ[key] for key in ('NIC', 'RP_EXP_DATA_ADDRESS', 'ARTIFACT_PEER'))
    route = json.loads(subprocess.check_output(['ip', '-j', 'route', 'get', other]))[0]
    if route.get('dev') != nic or route.get('prefsrc') != address:
        raise RuntimeError('Peer route must use the configured NIC and source IP')
    prefix = subprocess.check_output(['ros2', 'pkg', 'prefix', 'rp_exp_probe_effect'], text=True).strip()
    suffix = '_rel' if args.qos == 'reliable' else ''
    executable = 's2_sub' if args.worker == 'subscriber' else 's2_pub'
    argv = [str(Path(prefix) / 'lib/rp_exp_probe_effect' / (executable + suffix))]
    if args.worker == 'publisher':
        argv += ['200', str(200 * (args.seconds + 5))]
    env = dict(os.environ, RP_EXP_MEASURE_SEC=str(args.seconds))
    before = counters()
    started = time.time_ns()
    code = None
    try:
        code = subprocess.run(argv, env=env, timeout=args.seconds + 40).returncode
        if code:
            raise RuntimeError(f'Workload failed with exit {code}')
    finally:
        after = counters()
        data = dict(role=args.worker, qos=args.qos, seconds=args.seconds,
                    address=address, peer=other, nic=nic, route=route,
                    started_ns=started, ended_ns=time.time_ns(), returncode=code,
                    before=before, after=after,
                    deltas={name: {key: after[name][key] - before[name][key] for key in before[name]}
                            for name in before.keys() & after.keys()})
        output = Path(os.environ['RP_EXP_RESULTS_ROOT']) / '_master'
        (output / f'smoke-{args.qos}-{args.worker}-network.json').write_text(json.dumps(data, indent=2) + '\n')


def exchange(config, args, qos, output, reserve):
    jobs = []
    try:
        for role, worker in [('slave_b', 'subscriber'), ('slave_a', 'publisher')]:
            job = request(config, role, args.run_id, f'smoke-{qos}-{worker}',
                          ['python3', 'scripts/testbed_smoke.py', '--worker', worker,
                           '--seconds', str(args.seconds), '--qos', qos],
                          timeout=args.seconds + 50, reserve=reserve)
            job['env'].update(ROS_DOMAIN_ID='192', RP_EXP_EXECUTION_PROFILE='functional-smoke',
                              RP_EXP_DDS_MAX_MESSAGE_SIZE='32768')
            if worker == 'subscriber':
                job['ready_pattern'] = 'SUB_READY'
            remote = Remote(config['hosts'][role]['ssh'], job, output / (job['job_id'] + '.ssh.log'))
            jobs.append(remote)
            if worker == 'subscriber':
                wait_for(jobs, lambda codes: remote.ready or codes[0] is not None, 30)
                if not remote.ready or remote.proc.poll() is not None:
                    raise RuntimeError('Subscriber exited before readiness')
        wait_for(jobs, lambda codes: all(code is not None for code in codes), args.seconds + 60)
        identities = [job.identity for job in jobs]
        if not all(identities) or len(set(identities)) != 2:
            raise RuntimeError('Expected two distinct slave machines')
        if Path('/etc/machine-id').read_text().strip() in identities:
            raise RuntimeError('The master must not execute the ROS workload')
    finally:
        stop_all(jobs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=default_config())
    parser.add_argument('--link', choices=['wired', 'wireless'])
    parser.add_argument('--run-id', type=label, default='smoke-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    parser.add_argument('--seconds', type=int, default=10)
    parser.add_argument('--worker', choices=['publisher', 'subscriber'], help=argparse.SUPPRESS)
    parser.add_argument('--qos', choices=['best_effort', 'reliable'], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 2 <= args.seconds <= 30:
        parser.error('--seconds must be between 2 and 30 (functional checks only)')
    if args.worker:
        peer(args)
        return 0
    config = load(args.config, args.link)
    (ROOT / 'results').mkdir(exist_ok=True)
    with (ROOT / 'results/.orchestrator.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another master operation is active')
        destination = ROOT / 'results' / args.run_id
        destination.mkdir(exist_ok=False)
        output = destination / '_master'
        output.mkdir()
        report = dict(scope='two-slave S1 functionality; no rp, NTP scheduling or paper measurements',
                      status='failed', config=config, checks=[], seconds=args.seconds,
                      udp_max_message_size=32768)
        def interrupt(_sig, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        error = None
        try:
            for index, qos in enumerate(('best_effort', 'reliable')):
                print(f'[master] S1 /imu, {qos}, {args.seconds}s, {config["link"]}', flush=True)
                exchange(config, args, qos, output, reserve=index == 0)
        except (Exception, KeyboardInterrupt) as exc:
            error = repr(exc)
        try:
            collect(config, args.run_id, destination)
            if error:
                raise RuntimeError(error)
            for qos in ('best_effort', 'reliable'):
                logs = destination / 'slave_b/_master'
                text = (logs / f'smoke-{qos}-subscriber.log').read_text()
                if ('XMLPARSER' in text and 'Error' in text) or 'User transport failed to register' in text:
                    raise RuntimeError('Fast DDS transport initialization failed; inspect subscriber log')
                match = re.search(r'FINAL \[(\d+)s\]: recv (\d+) / expected (\d+)', text)
                if not match or int(match[1]) != args.seconds or int(match[2]) <= 0 or int(match[3]) != args.seconds * 200:
                    raise RuntimeError(f'Invalid or empty subscriber measurement: {qos}')
                for role in ROLES:
                    worker = 'publisher' if role == 'slave_a' else 'subscriber'
                    folder = destination / role / '_master'
                    text = (folder / f'smoke-{qos}-{worker}.log').read_text()
                    if ('XMLPARSER' in text and 'Error' in text) or 'User transport failed to register' in text:
                        raise RuntimeError(f'Fast DDS transport initialization failed on {role}')
                check = dict(qos=qos, received=int(match[2]), expected=int(match[3]), status='pass')
                report['checks'].append(check)
                print('[PASS] ' + json.dumps(check), flush=True)
            report['status'] = 'complete'
        except Exception as exc:
            report['error'] = repr(exc)
        finally:
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(f'[master] {report["status"]}: {output / "report.json"}', flush=True)
        return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
