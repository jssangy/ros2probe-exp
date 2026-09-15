"""Independent numeric and protocol regression checks for paper measurements."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from common.metrics import bandwidth, interval, resource_usage
from scripts.report_results import prepare, summary

ROOT = Path(__file__).resolve().parents[1]


def module(experiment):
    path = next((ROOT/'experiments').glob(experiment+'*/analysis/analyze.py'))
    spec = importlib.util.spec_from_file_location('analysis_'+experiment, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MeasurementWindow(unittest.TestCase):
    def test_bandwidth_clips_both_edges_and_weights_unequal_intervals(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'netdev.log'
            p.write_text('0 0\n1000 1000000\n2500 4000000\n3500 4500000\n')
            count, mbps, seconds, size = bandwidth(p, 500, 3000)
            self.assertEqual(count, 4)
            self.assertAlmostEqual(seconds, 2.5)
            self.assertEqual(size, 3750000)
            self.assertAlmostEqual(mbps, 12)

    def test_short_capture_reset_gap_and_duplicate_timestamp_rejected(self):
        for text in ['1000 0\n2000 100\n', '0 100\n1000 50\n2000 200\n',
                     '0 0\n5000 100\n', '0 0\n0 50\n2000 100\n']:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as d:
                p = Path(d)/'netdev.log'
                p.write_text(text)
                with self.assertRaises(ValueError):
                    bandwidth(p, 0, 2000)

    def test_missing_window_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'sub.log'
            p.write_text('FINAL [60s]: recv 12000 / expected 12000 -> drop 0%\n')
            with self.assertRaises(ValueError): interval(p)

    def test_resource_cpu_uses_the_same_interval_and_missing_pss_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'observer_cpu.log'
            p.write_text('0 0 100 1\n1000 10 200 1\n2000 30 300 1\n3000 50 400 1\n')
            value=resource_usage(p,500,2500)
            self.assertEqual(value['observer_cpu_avg'],30)
            self.assertEqual(value['observer_mem_avg_kb'],250)
            p.write_text('0 0 100 1\n1000 10 0 1\n2000 30 300 1\n3000 50 400 1\n')
            with self.assertRaisesRegex(ValueError,'PSS'):
                resource_usage(p,500,2500)

    def test_nan_sample_is_failure(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'netdev.log';p.write_text('0 0\n1000 nan\n2000 100\n')
            with self.assertRaisesRegex(ValueError,'Nonfinite'):
                bandwidth(p,0,2000)


class RunStatistics(unittest.TestCase):
    def test_stats_use_rows_and_sample_sd_not_summary_n(self):
        stats = summary([{'condition':'rp_hz','x':'2'}, {'condition':'rp_hz','x':'4'}], ['condition'], ['x'])[0]
        self.assertEqual(stats['n_runs'], 2)
        self.assertEqual(stats['x_mean'], 3)
        self.assertAlmostEqual(stats['x_sd'], 2**.5)

    def test_single_run_has_no_sd_and_missing_is_not_zero(self):
        one = summary([{'condition':'rp_bag','lost_recall':None}], ['condition'], ['lost_recall'])[0]
        self.assertIsNone(one['lost_recall_mean'])
        self.assertIsNone(one['lost_recall_sd'])
        self.assertEqual(one['lost_recall_n'], 0)

    def test_duplicate_run_rejected(self):
        row = {'scenario':'S1','condition':'baseline','run':'run01','rx_mbps':'1','drop_pct':'0'}
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            prepare(4, [row, row])

    def test_s7_recording_selects_all_topics_and_preserves_high_loss(self):
        raw = [dict(scenario='S7', condition=c, run='run01', rx_mbps='500', drop_pct='99.7', validity='valid')
               for c in ['rp_bag_one','rp_bag_all','topic_hz']]
        rows, excluded = prepare(4, raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(excluded), 1)
        self.assertEqual(rows[0]['condition'], 'rp_bag')
        self.assertEqual(rows[1]['drop_pct'], 99.7)

    def test_protocols_are_not_pooled(self):
        raw = [dict(scenario='S1', condition='baseline', run=f'run{i}', rx_mbps='1', drop_pct='0', protocol=p)
               for i,p in enumerate(['paper-v2','unverified'])]
        with self.assertRaisesRegex(ValueError,'Mixed'):
            prepare(4, raw)
        raw[1].pop('protocol')
        with self.assertRaisesRegex(ValueError,'Mixed'):
            prepare(4, raw)

    def test_missing_metric_cannot_count_toward_coverage(self):
        rows,excluded=prepare(4,[dict(scenario='S1',condition='baseline',run='run01',rx_mbps='',drop_pct='0')])
        self.assertFalse(rows)
        self.assertIn('rx_mbps',excluded[0]['reason'])

    def test_high_loss_flag_remains_in_resource_comparison(self):
        rows,excluded=prepare(2,[dict(platform='rpi',scenario='ST1000',condition='rp_hz',run='run01',
                            drop_pct='90',observer_cpu_norm_avg='7',observer_mem_avg_kb='1024',validity='startup_transient')])
        self.assertEqual(len(rows),1)
        self.assertFalse(excluded)

    def test_short_and_full_measurements_are_not_pooled(self):
        rows=[dict(scenario='S1',condition='baseline',run=f'run{i}',rx_mbps='1',drop_pct='0',
                   protocol='paper-v2',measure_start_ms='1000',measure_end_ms=str(end))
              for i,end in enumerate([4000,61000])]
        with self.assertRaisesRegex(ValueError,'durations'):
            prepare(4,rows)


class DiscoveryCounting(unittest.TestCase):
    def test_acknack_does_not_shift_next_data_writer(self):
        analyzer = module('01')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'capture.csv'
            fields = ['frame.number','frame.time_epoch','ip.src','ip.dst','rtps.sm.id',
                      'rtps.sm.wrEntityId','rtps.sm.rdEntityId','rtps.guidPrefix.src']
            with path.open('w', newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields,delimiter='\t')
                writer.writeheader()
                base={'frame.number':'1','frame.time_epoch':'10.5','ip.src':'192.0.2.1','ip.dst':'192.0.2.2',
                      'rtps.sm.id':'0x0e,0x06,0x15','rtps.sm.wrEntityId':'0x000003c2,0x000100c2',
                      'rtps.sm.rdEntityId':'0x000003c7,0x00000000','rtps.guidPrefix.src':'abcd'}
                writer.writerow(base)
                writer.writerow(dict(base, **{'frame.number':'2','frame.time_epoch':'9.5'}))
                writer.writerow(dict(base, **{'frame.number':'3','frame.time_epoch':'11.5'}))
            counts = analyzer._count_run(str(path), 11, 10)
            self.assertEqual(counts['spdp_count'], 1)
            self.assertEqual(counts['control_count'], 1)
            self.assertEqual(counts['discovery_packets'], 2)
            self.assertEqual(counts['rtps_packet_count'], 1)
            self.assertEqual(counts['discovery_packet_count'], 1)

    def test_epoch_boundaries_keep_timestamp_precision(self):
        analyzer = module('01')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'capture.csv'
            fields = ['frame.number', 'frame.time_epoch', 'rtps.sm.id',
                      'rtps.sm.wrEntityId', 'rtps.sm.rdEntityId']
            times = ['1790000000.999999999', '1790000001.000000000',
                     '1790000007.999999999', '1790000008.000000000']
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields, delimiter='\t')
                writer.writeheader()
                for i, timestamp in enumerate(times):
                    writer.writerow({'frame.number': i+1, 'frame.time_epoch': timestamp,
                                     'rtps.sm.id': '0x15', 'rtps.sm.wrEntityId': '0x000100c2',
                                     'rtps.sm.rdEntityId': '0x00000000'})
            counts = analyzer._count_run(str(path), 1790000008, 1790000001)
            self.assertEqual(counts['discovery_packet_count'], 2)
            self.assertEqual(counts['spdp_count'], 2)
            self.assertEqual(counts['first_discovery_packet_epoch'], times[1])
            self.assertEqual(counts['last_discovery_packet_epoch'], times[2])

    def test_missing_or_invalid_times_never_become_an_unbounded_count(self):
        analyzer = module('01')
        for start, end in [(None, 8), (1, None), (8, 1), (1, 1), ('NaN', 8), (1, 'Infinity')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                analyzer._count_run('unused.csv', end, start)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'capture.csv'
            for timestamp in ['', 'NaN', 'Infinity', 'bad']:
                with self.subTest(timestamp=timestamp):
                    path.write_text('frame.number\tframe.time_epoch\n1\t'+timestamp+'\n')
                    with self.assertRaises(ValueError):
                        analyzer._count_run(str(path), 8, 1)

    def test_analysis_uses_latest_match_on_both_slaves_and_excludes_tail(self):
        from test_discovery_completion import make_trial
        analyzer = module('01')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a = make_trial(root/'a', 'slave-a', ['100.100000'])
            b = make_trial(root/'b', 'slave-b', ['100.200000'])
            capture = a/'discovery_capture_N_P_1_N_E_2.csv'
            capture.write_text('frame.number\tframe.time_epoch\trtps.sm.id\trtps.sm.wrEntityId\trtps.sm.rdEntityId\n'
                               '1\t96\t0x15\t0x000100c2\t0x00000000\n'
                               '2\t100\t0x15\t0x000100c2\t0x00000000\n'
                               '3\t100.150000\t0x15\t0x000100c2\t0x00000000\n'
                               '4\t100.200000\t0x15\t0x000100c2\t0x00000000\n'
                               '5\t106\t0x15\t0x000100c2\t0x00000000\n')
            with self.assertRaisesRegex(ValueError, 'Missing peer matching evidence'):
                analyzer.run_analysis(str(root/'a'), str(root/'analysis'))
            with patch('scripts.report_results.build_report'):
                self.assertEqual(analyzer.run_analysis(str(root/'a'), str(root/'analysis'), str(root/'b')), 0)
            with (root/'analysis/runs.csv').open() as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['discovery_packet_count'], '2')
            self.assertEqual(row['measure_start'], '100')
            self.assertEqual(row['measure_end'], '100.200000')
            self.assertEqual(row['protocol'], 'discovery-matching-v1')
            self.assertEqual(row['first_discovery_packet_epoch'], '100')
            self.assertEqual(row['discovery_duration_ms'], '200.000000')

    def test_two_hosts_remain_separate_repetitions(self):
        analyzer=module('01')
        raw=[]
        for host in ['a','b']:
            raw.append(dict(root=host,scale='G1',condition='baseline',
                            **{m:1 for m in ['control_count','discovery_packets','participant_count','sedp_count','spdp_count']}))
        stats=analyzer._summarize(raw)
        self.assertEqual(len(stats),2)
        self.assertEqual([row['runs'] for row in stats], [1,1])


class FidelityEvidence(unittest.TestCase):
    def test_missing_sequence_log_does_not_become_100_percent_loss(self):
        analyzer=module('03')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            run=root/'loss10/rosbag2/run01'
            run.mkdir(parents=True)
            (run/'run_status.txt').write_text('complete\n')
            (run/'protocol_version.txt').write_text('paper-v2\n')
            with self.assertRaisesRegex(ValueError,'Missing subscriber sequence'):
                analyzer.collect(root, root/'bags')

    def test_loss_recall_undefined_for_no_actual_loss(self):
        self.assertIsNone(module('03').ratio(0,0))

    def test_missing_loss_acknowledgement_is_failure(self):
        analyzer=module('03')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); run=root/'loss10/rosbag2/run01'; run.mkdir(parents=True)
            (run/'run_status.txt').write_text('complete\n')
            (run/'protocol_version.txt').write_text('paper-v2\n')
            (run/'sub.log').write_text('CONFIG expected=1800\nSEQ 1\nFINAL [60s]: recv 1 / expected 1800 -> drop 99.9%\n')
            (run/'control.log').write_text('loss_acknowledged=0\npublisher_completed=1800\n')
            with self.assertRaisesRegex(ValueError,'acknowledgement'):
                analyzer.collect(root, root/'bags')


if __name__ == '__main__': unittest.main()
