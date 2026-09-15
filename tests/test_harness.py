"""Regression checks for false success, missing data, and START/READY races."""
import importlib.util
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sync', ROOT / 'common/sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class RunValidation(unittest.TestCase):
    def shell(self, body, *args):
        return subprocess.run(['bash', '-c', 'source "$1/common/validation.sh"; shift; ' + body,
                               'test', str(ROOT), *args], capture_output=True, text=True, timeout=10)

    def test_subscriber_exit_code_propagates(self):
        result = self.shell('(exit 17) & pid=$!; rp_exp_wait_subscriber "$pid" 3')
        self.assertEqual(result.returncode, 17, result.stderr)

    def test_subscriber_timeout_is_failure(self):
        result = self.shell('sleep 5 & pid=$!; rp_exp_wait_subscriber "$pid" 1; status=$?; kill "$pid"; wait "$pid" 2>/dev/null; exit "$status"')
        self.assertEqual(result.returncode, 1)

    def test_missing_zero_or_incomplete_composite_result_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'sub.log'
            for contents, expected, code in [('', '1', 1),
                    ('FINAL [30s]: recv 0 / expected 6000 -> drop 100.0%\n', '1', 1),
                    ('FINAL [30s]: recv 5900 / expected 6000 -> drop 1.7%\n', '1', 0),
                    ('FINAL [30s]: recv 5900 / expected 6000 -> drop 1.7%\n', '8', 1)]:
                with self.subTest(contents=contents, expected=expected):
                    log.write_text(contents)
                    result = self.shell('rp_exp_validate_subscriber "$1" "$2"', str(log), expected)
                    self.assertEqual(result.returncode, code, result.stderr)

    def test_dead_observer_fails(self):
        self.assertEqual(self.shell('rp_exp_require_live "" observer').returncode, 1)

    def test_custom_socket_rejected_before_sudo(self):
        self.assertEqual(self.shell('RP_SOCKET=/tmp/unsupported.sock; rp_exp_prepare_runtime').returncode, 1)

    def test_active_runtime_not_unlinked(self):
        result = self.shell('RP_BIN=/bin/true; rp_exp_prepare_runtime')
        self.assertEqual(result.returncode, 1)
        self.assertIn('already running', result.stderr)

    def test_background_observer_does_not_inherit_ignored_sigint(self):
        code = 'import signal,sys; sys.exit(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)'
        old = self.shell('setsid python3 -c "$1" & wait $!', code)
        fixed = self.shell('setsid env --default-signal=INT,QUIT python3 -c "$1" & wait $!', code)
        self.assertEqual(old.returncode, 1)
        self.assertEqual(fixed.returncode, 0, fixed.stderr)


class PublisherSync(unittest.TestCase):
    def exchange(self, reply):
        # Reserve ports using the OS so the test does not depend on experiment ports.
        with socket.socket() as server, socket.socket() as reservation:
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            reservation.bind(('127.0.0.1', 0))
            ack_port = reservation.getsockname()[1]
            reservation.close()
            errors = []

            def peer():
                try:
                    server.settimeout(3)
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(3)
                        command = bytearray()
                        while True:
                            chunk = conn.recv(1024)
                            if not chunk:
                                break
                            command.extend(chunk)
                    self.assertEqual(command, b'START S1 best_effort\n')
                    # Reply immediately: the old receiver bound its port too late.
                    with socket.create_connection(('127.0.0.1', ack_port), timeout=3) as ack:
                        ack.sendall(reply)
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=peer)
            thread.start()
            try:
                sync.start_publisher('127.0.0.1', server.getsockname()[1], ack_port,
                                     'START S1 best_effort', 3)
            finally:
                thread.join(4)
                self.assertFalse(thread.is_alive())
                if errors:
                    raise errors[0]

    def test_immediate_ready_succeeds(self):
        self.exchange(b'READY\n')

    def test_error_or_empty_reply_fails(self):
        for reply in (b'ERROR publisher exited\n', b''):
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                self.exchange(reply)

    def test_occupied_ack_port_prevents_start(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen(1)
            with self.assertRaises(OSError):
                sync.start_publisher('127.0.0.1', 1, occupied.getsockname()[1], 'START S1', 1)


class AnalysisValidation(unittest.TestCase):
    def test_incomplete_run_blocks_all_three_analyzers(self):
        cases = [
            ('02_resource_overhead', 'pc/ST100/baseline/run01', 'collect_runs'),
            ('03_observation_fidelity', 'loss00/rosbag2/run01', 'collect'),
            ('04_probe_effect', 'S1/baseline/run01', 'collect_runs'),
        ]
        for experiment, relative, function in cases:
            with self.subTest(experiment=experiment), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run = root / relative
                run.mkdir(parents=True)
                (run / 'run_status.txt').write_text('incomplete\n')
                module_spec = importlib.util.spec_from_file_location(
                    experiment, ROOT / 'experiments' / experiment / 'analysis/analyze.py')
                module = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(module)
                args = (root, root / 'bags') if function == 'collect' else (root,)
                with self.assertRaisesRegex(ValueError, 'Incomplete/failed run'):
                    getattr(module, function)(*args)


if __name__ == '__main__':
    unittest.main()
