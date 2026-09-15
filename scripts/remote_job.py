#!/usr/bin/env python3
"""Run one master-owned job on a slave; stop it when its SSH heartbeat is lost."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from dds_transport import configure as configure_transport
from network_guard import require_active, verify as verify_network


def listening(port):
    """Inspect listeners without making a connection to the command protocol."""
    for name in ('tcp', 'tcp6'):
        path = Path('/proc/net') / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            parts = line.split()
            if parts[3] == '0A' and int(parts[1].rsplit(':', 1)[1], 16) == port:
                return True
    return False


def stop_job(proc, grace=45):
    # Signal only the group created here. Its controllers own their child sessions
    # and get time to finalize bags, remove netem, and stop privileged observers.
    for sig, seconds in ((signal.SIGINT, grace), (signal.SIGTERM, 10), (signal.SIGKILL, 3)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            continue


def supervise(request, stdin_fd=0):
    repo = Path(request['repo']).expanduser().resolve()
    run_root = repo / 'results' / request['run_id']
    lock_path = repo / 'results' / '.master.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another master job is active on this slave') from exc
        if request.get('reserve'):
            run_root.mkdir(parents=True, exist_ok=False)
        logs = run_root / '_master'
        logs.mkdir(parents=True, exist_ok=True)
        stem = logs / request['job_id']
        metadata = dict(request, hostname=socket.gethostname(), started=time.time(), status='incomplete')
        guarded = request.get('env', {}).get('RP_EXP_REQUIRE_NETWORK_GUARD') == '1'
        if guarded:
            metadata['network_isolation'] = require_active(request['run_id'], repo)
            verify_network(metadata['network_isolation'])
        metadata_path = stem.with_suffix('.json')
        # Never overwrite evidence from an earlier attempt.
        with metadata_path.open('x') as stream:
            json.dump(metadata, stream, indent=2)
        ports = request.get('unused_ports', [])
        if any(listening(port) for port in ports):
            raise RuntimeError(f'Control port already occupied: {ports}')
        env = os.environ.copy()
        env.update(request.get('env', {}))
        env.update(ROS_SETUP=str(Path(request['ros_setup']).expanduser()),
                   RP_EXP_SUPERVISED='1',
                   WORKSPACE_SETUP=str(repo / 'install/setup.bash'),
                   RP_EXP_RESULTS_ROOT=str(run_root),
                   RP_EXP_BAGS_ROOT=str(repo / 'bags' / request['run_id']),
                   PYTHONUNBUFFERED='1', MPLBACKEND='Agg')
        metadata['dds_profile'] = configure_transport(env, stem.with_suffix('.dds.xml'))
        schedule_dir = logs / (request['job_id'] + '-schedules')
        if request.get('master_schedule'):
            schedule_dir.mkdir(exist_ok=False)
            env['RP_EXP_SCHEDULE_DIR'] = str(schedule_dir)
        # A has no recorder in Exp2–4; an empty bag root is still collectable.
        Path(env['RP_EXP_BAGS_ROOT']).mkdir(parents=True, exist_ok=True)
        machine_id = Path('/etc/machine-id').read_text().strip()
        print('MASTER_HOST ' + machine_id, flush=True)
        rp = request.get('rp_bin', '')
        if not rp:
            rp = str(repo / '.tools/bin/rp')
        if rp:
            rp = Path(rp).expanduser()
            if not rp.is_absolute():
                rp = repo / rp
            env['RP_BIN'] = str(rp)
            env['PATH'] = str(rp.parent) + os.pathsep + env.get('PATH', '')
        setup = ('set -e; source common/environment.sh; '
                 'rp_exp_source_setup "$ROS_SETUP"; ')
        if request.get('workspace', True):
            setup += 'rp_exp_source_setup "$WORKSPACE_SETUP"; '
        setup += 'unset ROS_LOCALHOST_ONLY ROS_AUTOMATIC_DISCOVERY_RANGE ROS_STATIC_PEERS; '
        setup += 'exec ' + shlex.join(request['argv'])
        stopped = False

        def interrupted(_signum, _frame):
            nonlocal stopped
            stopped = True

        handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        proc = None
        try:
            with stem.with_suffix('.log').open('x') as output:
                proc = subprocess.Popen(['bash', '-c', setup], cwd=repo, env=env,
                                        stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                started = last_heartbeat = time.monotonic()
                ready = False
                reason = ''
                input_buffer = b''
                announced = set()
                while proc.poll() is None:
                    if guarded:
                        try:
                            require_active(request['run_id'], repo)
                        except (OSError, ValueError, RuntimeError) as exc:
                            reason = 'Network isolation lost: ' + str(exc)
                            break
                    now = time.monotonic()
                    readable, _, _ = select.select([stdin_fd], [], [], 0.2)
                    if readable:
                        chunk = os.read(stdin_fd, 4096)
                        if chunk:
                            last_heartbeat = time.monotonic()
                            input_buffer += chunk
                            if len(input_buffer) > 65536:
                                raise RuntimeError('Oversized master control input')
                            while b'\n' in input_buffer:
                                line, input_buffer = input_buffer.split(b'\n', 1)
                                if line.startswith(b'{'):
                                    response = json.loads(line)
                                    token = response.get('id')
                                    if token not in announced or type(response.get('target_ns')) is not int:
                                        raise RuntimeError('Unexpected master schedule response')
                                    path = schedule_dir / (token + '.reply.json')
                                    if path.exists():
                                        raise RuntimeError('Duplicate master schedule response')
                                    temporary = path.with_suffix('.tmp')
                                    temporary.write_text(json.dumps(response))
                                    temporary.replace(path)
                        else:
                            reason = 'Master SSH input closed'
                            break
                    if stopped:
                        reason = 'Interrupted'
                        break
                    if now - last_heartbeat > request.get('heartbeat_timeout', 30):
                        reason = 'Master heartbeat expired'
                        break
                    if now - started > request['timeout']:
                        reason = 'Job deadline exceeded'
                        break
                    if request.get('master_schedule'):
                        for path in schedule_dir.glob('*.request.json'):
                            value = json.loads(path.read_text())
                            token = value['id']
                            if token not in announced:
                                if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
                                    raise RuntimeError('Invalid schedule request ID')
                                print('MASTER_SCHEDULE ' + json.dumps(value), flush=True)
                                announced.add(token)
                    port = request.get('ready_port')
                    pattern = request.get('ready_pattern')
                    log_ready = pattern and pattern in stem.with_suffix('.log').read_text(errors='replace')
                    if not ready and ((port and listening(port)) or log_ready):
                        print('MASTER_READY', flush=True)
                        ready = True
                if reason:
                    stop_job(proc, request.get('cleanup_grace', 45))
                code = proc.wait()
                if reason or ((request.get('ready_port') or request.get('ready_pattern')) and not ready):
                    code = code or 1
                metadata.update(ended=time.time(), returncode=code,
                                status='complete' if code == 0 else 'failed', error=reason)
        finally:
            if proc is not None:
                stop_job(proc, request.get('cleanup_grace', 45))
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
        print(json.dumps({'job': request['job_id'], 'status': metadata['status'],
                          'returncode': metadata.get('returncode'), 'log': str(stem.with_suffix('.log'))}), flush=True)
        return metadata['returncode']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', help='base64-encoded JSON from run_experiments.py')
    args = parser.parse_args()
    try:
        request = json.loads(base64.b64decode(args.request, validate=True))
        return supervise(request)
    except Exception as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
