#!/usr/bin/env python3
"""Hold the opposite physical link down for one master-owned experiment session."""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import time

STATE = Path('/run/ros2probe-exp-network.json')
ROOT = Path(__file__).resolve().parents[1]


def command(argv):
    try:
        return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT,
                                       timeout=45, env=dict(os.environ, LC_ALL='C')).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'{argv}: {exc.output.strip()}') from exc


def links():
    return {row['ifname']: row for row in json.loads(command(['ip', '-j', 'address']))}


def physical_links():
    return {p.name: 'wireless' if (p / 'wireless').exists() else 'wired'
            for p in Path('/sys/class/net').iterdir()
            if (p / 'device').exists() and (p / 'type').read_text().strip() == '1'}


def nm(nic, field):
    return command(['nmcli', '-g', 'GENERAL.' + field, 'device', 'show', nic])


def snapshot(nic, row):
    managed = nm(nic, 'NM-MANAGED') == 'yes'
    return dict(nic=nic, up='UP' in row['flags'], managed=managed,
                autoconnect=nm(nic, 'AUTOCONNECT'), connection=nm(nic, 'CON-UUID'),
                addresses=row.get('addr_info', []))


def plan(request):
    nic, address = request['nic'], str(ipaddress.IPv4Address(request['address']))
    current, physical = links(), physical_links()
    if physical.get(nic) != request['link']:
        raise RuntimeError('Configured NIC does not match the wired/wireless physical link type')
    if nic not in current or 'UP' not in current[nic]['flags'] or address not in [
            a.get('local') for a in current[nic].get('addr_info', [])]:
        raise RuntimeError('Selected NIC/IP must already be connected before isolation')
    connection = request['ssh_connection'].split()
    if len(connection) != 4 or connection[2] != address:
        raise RuntimeError('SSH must connect to the selected data IP; refusing to disable its other link')
    if request['master_machine_id'] == Path('/etc/machine-id').read_text().strip():
        raise RuntimeError('The master must not be a slave')
    disabled = [snapshot(name, current[name]) for name, kind in physical.items()
                if kind != request['link']]
    for item in disabled:
        if item['autoconnect'] not in ('yes', 'no'):
            raise RuntimeError('Cannot determine NetworkManager autoconnect state')
        if not item['managed'] and item['up'] and item['addresses']:
            raise RuntimeError('Cannot restore an unmanaged addressed NIC automatically')
    return dict(nic=nic, address=address, link=request['link'], disabled=disabled,
                before=current, run_id=request['run_id'], repo=str(ROOT), pid=os.getpid(),
                awaiting_ssh_confirmation=request.get('require_ssh_confirmation', False))


def disable(item):
    nic = item['nic']
    if item['managed']:
        # Runtime device settings only; do not edit saved connection profiles.
        command(['nmcli', 'device', 'set', nic, 'autoconnect', 'no'])
        command(['nmcli', 'device', 'set', nic, 'managed', 'no'])
    command(['ip', 'link', 'set', 'dev', nic, 'down'])


def restore(item):
    nic = item['nic']
    errors = []
    if item['managed']:
        try:
            command(['nmcli', 'device', 'set', nic, 'managed', 'yes'])
            if item['up']:
                command(['ip', 'link', 'set', 'dev', nic, 'up'])
            if item['connection'] not in ('', '--'):
                wait_available(nic)
                command(['nmcli', '--wait', '30', 'connection', 'up', 'uuid',
                         item['connection'], 'ifname', nic])
            command(['ip', 'link', 'set', 'dev', nic, 'up' if item['up'] else 'down'])
        except Exception as exc:
            errors.append(str(exc))
        finally:
            try:
                command(['nmcli', 'device', 'set', nic, 'autoconnect', item['autoconnect']])
            except Exception as exc:
                errors.append(str(exc))
    else:
        try:
            command(['ip', 'link', 'set', 'dev', nic, 'up' if item['up'] else 'down'])
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError('; '.join(errors))


def wait_available(nic, timeout=20):
    # managed=yes returns before Wi-Fi has transitioned from UNAVAILABLE (20)
    # to DISCONNECTED (30). A connection request during that transition fails.
    deadline = time.monotonic() + timeout
    while True:
        state = int(nm(nic, 'STATE').split()[0])
        if 30 <= state <= 100:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{nic}: NetworkManager device did not become available (state {state})')
        time.sleep(0.2)


def verify(state):
    current = links()
    active = current.get(state['nic'], {})
    if 'UP' not in active.get('flags', []) or state['address'] not in [
            a.get('local') for a in active.get('addr_info', [])]:
        raise RuntimeError('Selected data link lost its UP state or configured address')
    for item in state['disabled']:
        if 'UP' in current.get(item['nic'], {}).get('flags', []):
            raise RuntimeError('Excluded interface became UP: ' + item['nic'])
    return current


def announce_address(state, frames=2):
    """Refresh stale ARP entries before reusing SSH after an interface switch."""
    row = state['isolated'][state['nic']]
    mac = bytes.fromhex(row['address'].replace(':', ''))
    address = ipaddress.IPv4Address(state['address']).packed
    if len(mac) != 6 or mac[0] & 1 or mac == b'\0' * 6:
        raise RuntimeError('Selected interface has no usable Ethernet MAC address')
    # RFC 5227 ARP Announcement: broadcast request with sender IP == target IP.
    # Announce only this NIC's already verified IP, before any measurement starts.
    arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x0800, 6, 4, 1,
                      mac, address, b'\0' * 6, address)
    frame = (b'\xff' * 6 + mac + struct.pack('!H', 0x0806) + arp).ljust(60, b'\0')
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806)) as sock:
        sock.settimeout(3)
        sock.bind((state['nic'], 0))
        for index in range(frames):
            if index:
                time.sleep(2)
            if sock.send(frame) != len(frame):
                raise RuntimeError('Could not send complete ARP announcement')
    return dict(nic=state['nic'], address=state['address'], mac=row['address'], frames=frames)


def require_active(run_id, repo, state_path=STATE):
    state = json.loads(state_path.read_text())
    if (state.get('status') != 'active' or state.get('run_id') != run_id or
            state.get('repo') != str(Path(repo).resolve()) or
            not Path(f'/proc/{state["pid"]}').exists()):
        raise RuntimeError('Network isolation is not active for this run')
    return state


def save(state):
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2) + '\n')
    temporary.chmod(0o644)
    temporary.replace(STATE)
    try:
        print('NETWORK_STATE ' + json.dumps(state), flush=True)
    except BrokenPipeError:
        pass  # Losing the master must never prevent local restoration.


def heartbeat_loop(state, stdin_fd=0, timeout=30):
    last, checked, buffer = time.monotonic(), 0, b''
    announced = last
    while True:
        now = time.monotonic()
        # A broadcast can race with the opposite NIC's asynchronous disconnect.
        # Refresh only during setup; master confirms fresh SSH before preflight.
        if state.get('awaiting_ssh_confirmation') and now - announced >= 2:
            announce_address(state, frames=1)
            state['startup_arp_refreshes'] = state.get('startup_arp_refreshes', 0) + 1
            announced = now
        if now - checked >= 1:
            verify(state)
            checked = now
        ready, _, _ = select.select([stdin_fd], [], [], 0.2)
        if ready:
            data = os.read(stdin_fd, 4096)
            if not data:
                raise RuntimeError('Master network-guard SSH input closed')
            buffer += data
            if len(buffer) > 65536:
                raise RuntimeError('Oversized network guard input')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if line == b'restore':
                    return
                if line == b'heartbeat':
                    last = now
                if line == b'confirm-ssh' and state.get('awaiting_ssh_confirmation'):
                    state['awaiting_ssh_confirmation'] = False
                    state['ssh_confirmed_at'] = time.time()
                    save(state)
        if now - last > timeout:
            raise RuntimeError('Master network-guard heartbeat expired')


def wait_workers():
    # remote_job observes the stopping state and cancels its own process group.
    path = ROOT / 'results/.master.lock'
    path.parent.mkdir(exist_ok=True)
    with path.open('a') as lock:
        deadline = time.monotonic() + 65
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError('Worker cleanup did not finish before network restoration')
                time.sleep(0.2)


def hold(request):
    state = plan(request)  # Validate SSH and snapshot every NIC before any change.
    changed, errors = [], []
    previous = {}
    def interrupted(signum, frame):
        raise RuntimeError(f'Network guard interrupted by signal {signum}')
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, interrupted)
    try:
        state.update(status='preparing', started=time.time())
        save(state)
        for item in state['disabled']:
            changed.append(item)  # Includes partially applied changes on failure.
            disable(item)
        state['isolated'] = verify(state)
        state['arp_announcement'] = announce_address(state)
        state['status'] = 'active'
        save(state)
        print('MASTER_READY', flush=True)
        heartbeat_loop(state)
    except Exception as exc:
        errors.append(str(exc))
    finally:
        # A second cancellation must not interrupt restoration.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        state.update(status='restoring', errors=errors)
        save(state)
        try:
            wait_workers()
        except Exception as exc:
            errors.append(str(exc))
        for item in reversed(changed):
            try:
                restore(item)
            except Exception as exc:
                errors.append(item['nic'] + ': ' + str(exc))
        try:
            state['after'] = links()
        except Exception as exc:
            errors.append(str(exc))
        state.update(status='failed' if errors else 'restored', ended=time.time(), errors=errors)
        save(state)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('request')
    args = parser.parse_args()
    request = json.loads(base64.b64decode(args.request, validate=True))
    if not args.root:
        request['ssh_connection'] = os.environ.get('SSH_CONNECTION', '')
        encoded = base64.b64encode(json.dumps(request).encode()).decode()
        os.execvp('sudo', ['sudo', '-n', '--', 'python3', str(Path(__file__).resolve()), '--root', encoded])
    if os.geteuid() != 0:
        parser.error('Root mode requires sudo')
    with Path('/run/ros2probe-exp-network.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print('MASTER_HOST ' + Path('/etc/machine-id').read_text().strip(), flush=True)
        return hold(request)


if __name__ == '__main__':
    raise SystemExit(main())
