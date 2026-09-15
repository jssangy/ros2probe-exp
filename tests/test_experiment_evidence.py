"""Failure cases that must never turn into valid experiment measurements."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from common import metrics, validate_bag
from common.check_measurements import check
from test_paper_metrics import module

ROOT = Path(__file__).resolve().parents[1]


def timed_log(topic='/imu', received=600, expected=600, duration=3):
    return (f'CONFIG topic={topic} hz=200.000 warmup_sec=0 measure_sec={duration} qos=best_effort\n'
            'SUB_READY\nMEASURE_START_MS 1000\n'
            f'MEASURE_END_MS {1000 + duration * 1000}\n'
            f'TOPIC_RESULT topic={topic} recv={received} expected={expected} duration={duration}\n'
            f'FINAL [{duration}s]: recv {received} / expected {expected} -> drop 0.0%\n')


class RunEvidence(unittest.TestCase):
    def test_missing_completion_or_unknown_protocol_blocks_each_analyzer(self):
        layouts = {'01': 'host/G1/baseline/run_01', '02': 'pc/ST100/baseline/run01',
                   '03': 'loss0/rosbag2/run01', '04': 'S1/baseline/run01'}
        for exp, layout in layouts.items():
            with self.subTest(exp=exp), tempfile.TemporaryDirectory() as d:
                root = Path(d); run = root/layout; run.mkdir(parents=True)
                (run/'discovery_capture_N_P_1_N_E_2.csv').touch()
                analyzer = module(exp)
                def collect():
                    if exp == '01': return analyzer.run_analysis(str(root), str(root/'analysis'))
                    if exp == '03': return analyzer.collect(root, root/'bags')
                    return analyzer.collect_runs(root)
                with self.assertRaisesRegex(ValueError, 'completion evidence'): collect()
                (run/'run_status.txt').write_text('complete\n')
                (run/'protocol_version.txt').write_text('unexpected-protocol\n')
                with self.assertRaisesRegex(ValueError, 'unsupported measurement protocol'): collect()

    def test_single_run_sd_is_undefined_in_each_raw_summary(self):
        discovery = module('01')
        self.assertIsNone(discovery._stdev([10]))
        for exp in ['02', '03', '04']:
            row = dict(platform='pc', scenario='S1', condition='rosbag2', validity='valid', loss_pct=0,
                       rx_mbps=10, actual_drop_pct=0, observer_drop_pct=0, recv_jaccard=1,
                       lost_jaccard=None, lost_precision=None, lost_recall=None)
            stats = module(exp).summarize([row])[0]
            key = 'actual_drop_pct' if exp == '03' else 'rx_mbps'
            self.assertIsNone(stats[key+'_std'])
            self.assertEqual(stats[key+'_n'], 1)

    def test_missing_cache_loss_report_is_not_zero_loss(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d)/'obs.log'
            log.write_text('Recording...\nCache buffers lost messages\n')
            self.assertIsNone(module('02').parse_recorder_cache_loss(log))
            log.write_text('Total lost: 0\n')
            self.assertEqual(module('02').parse_recorder_cache_loss(log), 0)

    def test_final_count_and_interval_must_agree_with_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d)/'sub.log'
            for text in [timed_log(expected=599), timed_log().replace('END_MS 4000', 'END_MS 3999'),
                         timed_log() + 'FINAL [3s]: recv 600 / expected 600\n',
                         timed_log().replace('TOPIC_RESULT topic=/imu', 'TOPIC_RESULT topic=/wrong')]:
                with self.subTest(text=text):
                    log.write_text(text)
                    with self.assertRaises(ValueError): metrics.timed_subscribers(log)
            log.write_text(timed_log(received=0))
            self.assertEqual(metrics.timed_subscribers(log), {'/imu'})

    def test_launch_prefixed_subscribers_are_validated_independently(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d)/'sub.log'
            a = timed_log('/imu').splitlines()
            b = timed_log('/cmd_vel').splitlines()
            log.write_text('\n'.join(f'[{prefix}] {line}' for pair in zip(a, b)
                                     for prefix, line in zip(['imu-1', 'cmd-2'], pair)))
            self.assertEqual(metrics.timed_subscribers(log, 2), {'/imu', '/cmd_vel'})

    def test_alive_but_silent_rate_observer_fails_completion(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run/'sub.log').write_text(timed_log())
            (run/'platform.log').write_text('scenario=S1\ncondition=topic_hz\n')
            (run/'netdev.log').write_text('0 0\n1000 100\n2000 200\n3000 300\n4000 400\n')
            (run/'obs.log').write_text('waiting for data\n')
            with self.assertRaisesRegex(ValueError, 'rate observer samples'): check(run)
            (run/'obs.log').write_text('average rate: 199.9\n')
            check(run)

    def test_fidelity_rejects_inconsistent_or_foreign_sequences(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d)/'sub.log'
            for seq, received in [(4, 1), (1, 2), (0, 1)]:
                log.write_text(f'CONFIG expected=3\nSEQ {seq}\nFINAL [60s]: recv {received} / expected 3\n')
                with self.subTest(seq=seq, received=received), self.assertRaises(ValueError):
                    metrics.fidelity_subscriber(log)

    def test_fidelity_does_not_silently_clip_foreign_bag_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); run = root/'loss0/rosbag2/run01'; run.mkdir(parents=True)
            (run/'run_status.txt').write_text('complete\n')
            (run/'protocol_version.txt').write_text('paper-v2\n')
            (run/'sub.log').write_text('CONFIG expected=3\nSEQ 1\nFINAL [60s]: recv 1 / expected 3\n')
            (run/'platform.log').write_text('loss_pct=0\nexpected=3\n')
            (run/'control.log').write_text('loss_acknowledged=0\npublisher_completed=3\n')
            analyzer = module('03')
            with patch.object(analyzer, 'extract_bag_seqs', return_value={1, 4}):
                with self.assertRaisesRegex(ValueError, 'Out-of-range recorder'):
                    analyzer.collect(root, root/'bags')


class ReadinessAndRecording(unittest.TestCase):
    def test_readiness_waits_for_marker_and_rejects_dead_or_silent_process(self):
        script = '''source "$1/common/validation.sh"
log="$2/ready.log"
(sleep 0.3; echo SUB_READY > "$log"; sleep 5) &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT
rp_exp_wait_log "$pid" "$log" SUB_READY subscriber 1 3 || exit 1
test -s "$log" || exit 2
if rp_exp_wait_log "$pid" "$log" NO_MARKER silent 1 1; then exit 3; fi
kill "$pid"; wait "$pid" 2>/dev/null || true
if rp_exp_wait_log "$pid" "$log" SUB_READY dead 1 1; then exit 4; fi
'''
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(['bash', '-c', script, 'test', str(ROOT), d],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_corrupt_tail_after_required_topic_still_fails_bag_validation(self):
        class Reader:
            def __init__(self): self.count = 0
            def open(self, *args): pass
            def has_next(self): return True
            def read_next(self):
                self.count += 1
                if self.count == 1: return '/imu', b'cdr', 1
                raise RuntimeError('corrupt tail')
        fake = types.SimpleNamespace(SequentialReader=Reader, StorageOptions=lambda **k: None,
                                     ConverterOptions=lambda *a: None)
        with tempfile.TemporaryDirectory() as d, patch.dict(sys.modules, rosbag2_py=fake), \
                patch.object(sys, 'argv', ['validate_bag.py', d, '/imu']), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(validate_bag.main(), 1)
            self.assertIn('corrupt tail', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
