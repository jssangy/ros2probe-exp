"""Network changes must preserve SSH, roll back failures and fail closed."""
import copy
import json
import os
import ipaddress
import struct
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import network_guard as guard
import run_experiments as master


def interfaces():
    return {'eno1': dict(flags=['UP'], addr_info=[dict(local='192.0.2.10')]),
            'wlan0': dict(flags=['UP'], addr_info=[dict(local='192.0.2.20')])}


def item(nic):
    return dict(nic=nic, up=True, managed=True, autoconnect='yes',
                connection='saved-uuid', addresses=[])


class Isolation(unittest.TestCase):
    def make_plan(self, link, nic, address, ssh_address=None):
        request = dict(link=link, nic=nic, address=address, run_id='trial',
                       ssh_connection=f'192.0.2.1 45000 {ssh_address or address} 22',
                       master_machine_id='another-machine')
        with patch.object(guard, 'links', return_value=interfaces()), \
             patch.object(guard, 'physical_links', return_value={'eno1':'wired', 'wlan0':'wireless'}), \
             patch.object(guard, 'snapshot', side_effect=lambda nic, row: item(nic)):
            return guard.plan(request)

    def test_both_link_modes_select_only_the_opposite_device(self):
        for link, nic, address, other in [('wired','eno1','192.0.2.10','wlan0'),
                                          ('wireless','wlan0','192.0.2.20','eno1')]:
            with self.subTest(link=link):
                state = self.make_plan(link, nic, address)
                self.assertEqual([x['nic'] for x in state['disabled']], [other])

    def test_ssh_over_opposite_interface_is_rejected_before_mutation(self):
        with patch.object(guard, 'disable') as disable, self.assertRaisesRegex(RuntimeError, 'SSH must'):
            self.make_plan('wired','eno1','192.0.2.10','192.0.2.20')
        disable.assert_not_called()

    def test_stale_address_and_wrong_link_label_fail(self):
        for link, address in [('wired','192.0.2.99'),('wireless','192.0.2.10')]:
            with self.subTest(link=link), self.assertRaises(RuntimeError):
                self.make_plan(link,'eno1',address)

    def test_inactive_link_reappearing_or_active_address_lost_fails(self):
        state = self.make_plan('wired','eno1','192.0.2.10')
        current = interfaces()
        with patch.object(guard, 'links', return_value=current), self.assertRaisesRegex(RuntimeError, 'became UP'):
            guard.verify(state)
        current['wlan0']['flags'] = []
        with patch.object(guard, 'links', return_value=current):
            guard.verify(state)
        current['eno1']['addr_info'] = []
        with patch.object(guard, 'links', return_value=current), self.assertRaisesRegex(RuntimeError, 'address'):
            guard.verify(state)

    def test_normal_stop_and_partial_setup_and_ssh_failure_all_restore(self):
        for mode in ('normal','partial-disable','arp-failed','ssh-eof'):
            state = self.make_plan('wired','eno1','192.0.2.10')
            states = []
            with self.subTest(mode=mode), patch.object(guard,'plan',return_value=state), \
                 patch.object(guard,'save',side_effect=lambda s: states.append(copy.deepcopy(s))), \
                 patch.object(guard,'disable',side_effect=RuntimeError('nmcli failed') if mode=='partial-disable' else None), \
                 patch.object(guard,'verify',return_value={}), \
                 patch.object(guard,'announce_address',side_effect=RuntimeError('ARP failed') if mode=='arp-failed' else None), \
                 patch.object(guard,'heartbeat_loop',side_effect=RuntimeError('SSH EOF') if mode=='ssh-eof' else None), \
                 patch.object(guard,'wait_workers') as wait, \
                 patch.object(guard,'restore') as restore, patch.object(guard,'links',return_value={}):
                code = guard.hold({})
            restore.assert_called_once_with(state['disabled'][0])
            wait.assert_called_once()
            self.assertEqual(code,0 if mode=='normal' else 1)
            self.assertEqual(states[-2]['status'],'restoring')
            self.assertEqual(states[-1]['status'],'restored' if mode=='normal' else 'failed')

    def test_restore_failure_is_recorded(self):
        state = self.make_plan('wired','eno1','192.0.2.10')
        with patch.object(guard,'plan',return_value=state), patch.object(guard,'save'), \
             patch.object(guard,'disable'), patch.object(guard,'verify',return_value={}), \
             patch.object(guard,'announce_address'), \
             patch.object(guard,'heartbeat_loop'), patch.object(guard,'wait_workers'), \
             patch.object(guard,'restore',side_effect=RuntimeError('AP unavailable')), \
             patch.object(guard,'links',return_value={}):
            self.assertEqual(guard.hold({}),1)
        self.assertEqual(state['status'],'failed')
        self.assertIn('AP unavailable',str(state['errors']))

    def test_arp_announces_only_selected_interface_and_its_verified_address(self):
        state = dict(nic='wlan0', address='192.0.2.20',
                     isolated={'wlan0': dict(address='02:00:00:00:00:20')})
        with patch.object(guard.socket, 'socket') as factory, patch.object(guard.time, 'sleep'):
            sock = factory.return_value.__enter__.return_value
            sock.send.return_value = 60
            result = guard.announce_address(state)
        sock.bind.assert_called_once_with(('wlan0', 0))
        self.assertEqual(sock.send.call_count, 2)
        frame = sock.send.call_args.args[0]
        self.assertEqual(frame[:6], b'\xff' * 6)
        self.assertEqual(frame[6:12], bytes.fromhex('020000000020'))
        self.assertEqual(frame[12:14], b'\x08\x06')
        arp = struct.unpack('!HHBBH6s4s6s4s', frame[14:42])
        self.assertEqual(arp[:5], (1, 0x0800, 6, 4, 1))
        self.assertEqual(arp[5], frame[6:12])
        self.assertEqual(arp[6], ipaddress.IPv4Address(state['address']).packed)
        self.assertEqual(arp[7], b'\0' * 6)
        self.assertEqual(arp[8], arp[6])
        self.assertEqual(result['frames'], 2)

    def test_setup_arp_refresh_stops_before_confirmation_acknowledgement(self):
        state = dict(awaiting_ssh_confirmation=True)
        with patch.object(guard.time, 'monotonic', side_effect=[0,3,6,9]), \
             patch.object(guard.time, 'time', return_value=100), \
             patch.object(guard.select, 'select', return_value=([42],[],[])), \
             patch.object(guard.os, 'read', side_effect=[b'confirm-ssh\n',b'heartbeat\n',b'restore\n']), \
             patch.object(guard, 'verify'), patch.object(guard, 'save') as save, \
             patch.object(guard, 'announce_address') as announce:
            guard.heartbeat_loop(state, stdin_fd=42)
        announce.assert_called_once_with(state, frames=1)
        self.assertFalse(state['awaiting_ssh_confirmation'])
        self.assertEqual(state['ssh_confirmed_at'],100)
        self.assertEqual(state['startup_arp_refreshes'],1)
        save.assert_called_once_with(state)

    def test_fresh_ssh_requires_two_consecutive_successful_rounds(self):
        with tempfile.TemporaryDirectory() as directory:
            config = dict(hosts={r:dict(ssh=r) for r in master.ROLES})
            session = master.NetworkSession(config,'trial',Path(directory))
            session.jobs = [SimpleNamespace(pending_input=b'',network_states=[dict(ssh_confirmed_at=1)]) for _ in master.ROLES]
            calls=[]
            def probe(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 255 if len(calls)<=2 else 0)
            with patch.object(master.subprocess,'run',side_effect=probe), patch.object(master.time,'sleep'):
                session.confirm_ssh()
            self.assertEqual(len(calls),6)
            self.assertTrue(all(j.pending_input == b'confirm-ssh\n' for j in session.jobs))
            report=json.loads((Path(directory)/'network-ssh.json').read_text())
            self.assertEqual(report['status'],'complete')
            self.assertEqual(len(report['attempts']),3)

    def test_persistent_ssh_failure_cannot_confirm_network_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            session=master.NetworkSession(dict(hosts={r:dict(ssh=r) for r in master.ROLES}),'trial',Path(directory))
            session.jobs=[SimpleNamespace(pending_input=b'',network_states=[]) for _ in master.ROLES]
            with patch.object(master.time,'monotonic',side_effect=[0,0,0,21]), \
                 patch.object(master.time,'sleep'), \
                 patch.object(master.subprocess,'run',return_value=subprocess.CompletedProcess([],255)), \
                 self.assertRaisesRegex(RuntimeError,'measurement not started'):
                session.confirm_ssh()
            self.assertTrue(all(j.pending_input == b'' for j in session.jobs))
            report=json.loads((Path(directory)/'network-ssh.json').read_text())
            self.assertEqual(report['status'],'failed')

    def test_restore_recovers_device_autoconnect_even_if_connection_fails(self):
        def fail_connection(argv):
            if 'connection' in argv:
                raise RuntimeError('AP unavailable')
            return ''
        with patch.object(guard,'command',side_effect=fail_connection) as command, \
             patch.object(guard,'wait_available'):
            with self.assertRaisesRegex(RuntimeError,'AP unavailable'):
                guard.restore(item('wlan0'))
        self.assertEqual(command.call_args.args[0],['nmcli','device','set','wlan0','autoconnect','yes'])

    def test_wifi_reconnection_waits_for_networkmanager_device_readiness(self):
        with patch.object(guard,'nm',side_effect=['20 (unavailable)','20 (unavailable)','30 (disconnected)']) as nm, \
             patch.object(guard.time,'sleep') as sleep:
            guard.wait_available('wlan0')
        self.assertEqual(nm.call_count,3)
        self.assertEqual(sleep.call_count,2)
        with patch.object(guard,'nm',return_value='20 (unavailable)'), self.assertRaisesRegex(RuntimeError,'did not become available'):
            guard.wait_available('wlan0',timeout=0)

    def test_protocol_restore_eof_and_missing_heartbeat(self):
        for mode in ('restore','eof','expired'):
            read, write = os.pipe()
            try:
                if mode=='restore': os.write(write,b'heartbeat\nrestore\n')
                if mode=='eof': os.close(write); write=None
                with patch.object(guard,'verify'):
                    if mode=='restore': guard.heartbeat_loop({},read,timeout=0.01)
                    else:
                        with self.assertRaises(RuntimeError):
                            guard.heartbeat_loop({},read,timeout=0.01)
            finally:
                os.close(read)
                if write is not None: os.close(write)

    def test_worker_rejects_restoring_wrong_run_or_missing_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            state = dict(status='active',run_id='trial',repo=directory,pid=os.getpid())
            path.write_text(json.dumps(state))
            guard.require_active('trial',directory,path)
            for changes in ({'status':'restoring'},{'run_id':'different'},{'pid':999999999}):
                path.write_text(json.dumps(dict(state,**changes)))
                with self.assertRaises(RuntimeError): guard.require_active('trial',directory,path)


if __name__ == '__main__':
    unittest.main()
