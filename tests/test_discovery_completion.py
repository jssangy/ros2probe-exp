"""A missing or late peer match must not silently select a shorter/fixed window."""
import json
from pathlib import Path
import tempfile
import unittest

from common.discovery import PROTOCOL, paired_window, read_run
from common.metrics import require_run_metadata


def make_trial(root, host, times, protocol='paper-v2'):
    np = len(times)
    folder = Path(root) / host / f'G{np}/baseline/run_01'
    folder.mkdir(parents=True)
    meta = dict(protocol=protocol, host=host, N_P=np, N_E=2, total_processes=2*np,
                matched_processes=np, condition='baseline', ros_domain_id=77,
                measure_start=100, capture_start=95)
    if protocol == 'paper-v2':
        meta['measure_end'] = 107
    else:
        meta.update(capture_deadline=107, local_discovery_complete=max(times))
    (folder/'run_metadata.json').write_text(json.dumps(meta))
    (folder/'run_status.txt').write_text('complete\n')
    (folder/'protocol_version.txt').write_text(protocol+'\n')
    (folder/f'discovery_capture_N_P_{np}_N_E_2.csv').touch()
    for i, time in enumerate(times):
        (folder/f'node_{i}_topic_endpoint_count.csv').write_text(
            'time_epoch,stress_topic_0_pub,stress_topic_0_sub,total_pub,total_sub\n'
            + time + ',' + ','.join([str(2*np)]*4) + '\n')
        (folder/f'participant_{i}.log').write_text('[Step 3] Matched: completion epoch='+time+'\n')
    return folder


class DiscoveryCompletion(unittest.TestCase):
    def test_latest_of_all_processes_and_hosts_sets_common_end(self):
        with tempfile.TemporaryDirectory() as d:
            a = make_trial(Path(d)/'a', 'a', ['100.010000', '100.020000', '100.030000'])
            b = make_trial(Path(d)/'b', 'b', ['100.015000', '100.080000', '100.025000'])
            first, second = paired_window(a, b), paired_window(b, a)
            self.assertEqual(first['measure_end'], '100.080000')
            self.assertEqual(first['measure_end'], second['measure_end'])
            self.assertEqual(first['local_discovery_complete'], '100.030000')
            self.assertEqual(len(first['local_participant_completion']), 3)
            self.assertEqual(first['capture_protocol'], 'paper-v2')

    def test_new_protocol_uses_capture_deadline_only_for_validation(self):
        with tempfile.TemporaryDirectory() as d:
            a = make_trial(Path(d)/'a', 'a', ['100.010000'], PROTOCOL)
            b = make_trial(Path(d)/'b', 'b', ['100.020000'], PROTOCOL)
            self.assertEqual(paired_window(a, b)['measure_end'], '100.020000')
            with self.assertRaisesRegex(ValueError, 'unsupported measurement protocol'):
                require_run_metadata(a)  # Other experiments still accept only their own protocol.

    def test_missing_match_or_disagreeing_log_is_an_error(self):
        for mutation in ('missing', 'duplicate', 'log', 'counts', 'deadline'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                a = make_trial(Path(d)/'a', 'a', ['100.010000'])
                report = a/'node_0_topic_endpoint_count.csv'
                if mutation == 'missing': report.unlink()
                if mutation == 'duplicate': report.write_text(report.read_text()+report.read_text().splitlines()[-1]+'\n')
                if mutation == 'log': (a/'participant_0.log').write_text('missing match\n')
                if mutation == 'counts': report.write_text(report.read_text().replace(',2,2,2,2', ',1,2,1,2'))
                if mutation == 'deadline': report.write_text(report.read_text().replace('100.010000', '107.000000'))
                with self.assertRaises(ValueError): read_run(a)

    def test_wrong_peer_trial_or_failed_peer_is_an_error(self):
        for mutation in ('start', 'condition', 'protocol', 'same_host', 'failed'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as d:
                a = make_trial(Path(d)/'a', 'a', ['100.010000'])
                b = make_trial(Path(d)/'b', 'b', ['100.020000'], PROTOCOL if mutation == 'protocol' else 'paper-v2')
                path = b/'run_metadata.json'
                meta = json.loads(path.read_text())
                if mutation == 'start': meta['measure_start'] = 99
                if mutation == 'condition': meta['condition'] = 'rp_run'
                if mutation == 'same_host': meta['host'] = 'a'
                if mutation == 'failed': (b/'run_status.txt').write_text('failed\n')
                path.write_text(json.dumps(meta))
                with self.assertRaises(ValueError): paired_window(a, b)


if __name__ == '__main__':
    unittest.main()
