"""Paper-sized CSV export, profile separation and incomplete-batch rejection."""
import csv
import json
from pathlib import Path
from statistics import stdev
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_experiments as master
from testbed import analysis_jobs, load, time_budget
from export_results import export_results, result_paths


class ResultExport(unittest.TestCase):
    def fixture(self, directory, experiment, link='wired', run_id='fixture'):
        config = load(ROOT / 'testbed.example.json', link)
        args = master.parser().parse_args(['run', experiment, '--run-id', run_id])
        destination = Path(directory) / run_id
        jobs = analysis_jobs(config, args, destination)
        for job in jobs:
            rows = []
            if experiment == '1':
                for np in (1, 3, 5):
                    for condition in ('baseline', 'rp_run', 'ros2_daemon'):
                        for rep in range(1, 11):
                            value = rep + (100 if job['role'] == 'slave_b' else 0)
                            rows.append(dict(root=job['role'], N_P=np, condition=condition, rep=rep,
                                             protocol='discovery-matching-v1', spdp_count=value,
                                             sedp_count=10, control_count=2, discovery_packets=999,
                                             discovery_packet_count=rep, discovery_duration_ms=100))
            elif experiment == '2':
                for scenario in ('ST100', 'ST500', 'ST1000'):
                    for condition in ('baseline', 'rp_hz', 'topic_hz', 'rp_bag', 'rosbag2'):
                        for rep in range(1, 11):
                            rows.append(dict(platform='pc', scenario=scenario, condition=condition,
                                             run=f'run{rep:02d}', protocol='paper-v2', drop_pct=99.7,
                                             observer_cpu_norm_avg=rep, observer_mem_avg_kb=2048))
            elif experiment == '3':
                for loss in (0, 10, 20):
                    for condition in ('rp_bag', 'rosbag2'):
                        for rep in range(1, 11):
                            rows.append(dict(loss_pct=loss, condition=condition, run=f'run{rep:02d}',
                                             protocol='paper-v2', actual_drop_pct=loss, observer_drop_pct=loss,
                                             lost_recall=1 if loss else '', observer_extra_recv_count=0,
                                             observer_missed_recv_count=0))
            else:
                for scenario in (f'S{i}' for i in range(1, 8)):
                    for condition in ('baseline', 'rp_hz', 'topic_hz', 'rp_bag_all', 'rosbag2_all'):
                        for rep in range(1, 11):
                            rows.append(dict(link=link, qos=job['qos'], scenario=scenario,
                                             condition=condition, run=f'run{rep:02d}', protocol='paper-v2',
                                             rx_mbps=rep + (100 if job['qos'] == 'reliable' else 0),
                                             drop_pct=99.7, measure_start_ms=1000, measure_end_ms=61000))
            self.write_rows(job, rows)
        return config, args, destination, jobs

    def write_rows(self, job, rows):
        path = Path(job['runs_csv'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def read(self, path):
        with Path(path).open(newline='') as stream:
            return list(csv.DictReader(stream))

    def test_full_defaults_export_all_groups_without_pooling_slaves_or_qos(self):
        for experiment, link, count in [('1', 'wired', 18), ('2', 'wired', 15),
                                       ('3', 'wired', 6), ('4', 'wired', 70), ('4', 'wireless', 70)]:
            with self.subTest(experiment=experiment, link=link), tempfile.TemporaryDirectory() as directory:
                config, args, dest, jobs = self.fixture(directory, experiment, link)
                target = export_results(config, args, dest, jobs)[experiment]
                filename = f'experiment{experiment}.csv' if experiment != '4' else (
                    'experiment4-wire.csv' if link == 'wired' else 'experiment4-wireless.csv')
                self.assertEqual(target, Path(directory) / filename)
                self.assertFalse((dest / 'results.csv').exists())
                rows = self.read(target)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(row['n_runs'] == '10' for row in rows))
                self.assertEqual({row['run_id'] for row in rows}, {'fixture'})
                self.assertFalse(any(p.suffix in ('.png', '.pdf', '.tex', '.md') for p in dest.rglob('*')))
                if experiment == '1':
                    a = next(r for r in rows if r['slave'] == 'slave_a')
                    b = next(r for r in rows if r['slave'] == 'slave_b')
                    self.assertEqual(float(a['spdp_mean']), 5.5)
                    self.assertEqual(float(b['spdp_mean']), 105.5)
                    self.assertEqual(float(a['discovery_messages_mean']), 17.5)
                    self.assertAlmostEqual(float(a['spdp_sd']), stdev(range(1, 11)))
                elif experiment == '2':
                    self.assertEqual([r['scenario'] for r in rows[::5]], ['ST100', 'ST500', 'ST1000'])
                    self.assertEqual([r['condition'] for r in rows[:5]],
                                     ['baseline', 'rp_hz', 'topic_hz', 'rp_bag', 'rosbag2'])
                    self.assertTrue(all(float(r['pss_mib_mean']) == 2 for r in rows))
                    self.assertTrue(all(float(r['subscriber_drop_pct_mean']) == 99.7 for r in rows))
                elif experiment == '3':
                    zero = [r for r in rows if r['injected_loss_pct'] == '0']
                    self.assertTrue(all(r['loss_recall_mean'] == 'N/A' and r['loss_recall_n'] == '0' for r in zero))
                    self.assertTrue(all(r['loss_recall_mean'] == '1.0' for r in rows if r not in zero))
                else:
                    self.assertEqual({r['link'] for r in rows}, {link})
                    self.assertEqual({r['qos'] for r in rows}, {'best_effort', 'reliable'})
                    for row in rows:
                        self.assertEqual(float(row['rx_mbps_mean']), 105.5 if row['qos'] == 'reliable' else 5.5)
                        self.assertAlmostEqual(float(row['rx_mbps_sd']), stdev(range(1, 11)))

    def test_incomplete_or_mislabeled_evidence_never_publishes_a_csv(self):
        for mutation in ('missing_condition', 'missing_run', 'duplicate_run', 'wrong_qos', 'nan_metric'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                config, args, dest, jobs = self.fixture(directory, '4')
                rows = self.read(jobs[-1]['runs_csv'])
                if mutation == 'missing_condition': rows = [r for r in rows if r['scenario'] != 'S7']
                if mutation == 'missing_run': rows.pop()
                if mutation == 'duplicate_run': rows.append(rows[-1])
                if mutation == 'wrong_qos':
                    for row in rows: row['qos'] = 'best_effort'
                if mutation == 'nan_metric': rows[-1]['rx_mbps'] = 'nan'
                self.write_rows(jobs[-1], rows)
                with self.assertRaises(ValueError):
                    export_results(config, args, dest, jobs)
                self.assertTrue(all(not path.exists() for path in result_paths(config, args, dest).values()))

    def test_failed_reanalysis_preserves_previous_successful_csv_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, args, dest, _ = self.fixture(root / 'results', '4')
            (dest / '_master').mkdir()
            saved = dict(config=config, arguments={k: str(v) if isinstance(v, Path) else v
                                                   for k, v in vars(args).items()}, status='complete',
                         measurement_status='complete', export_status='complete', collection_status='complete')
            manifest = dest / '_master/run.json'
            manifest.write_text(json.dumps(saved))
            previous = dest.parent / 'experiment4-wire.csv'
            previous.write_text('previous successful batch\n')
            cli = master.parser()
            analyze = cli.parse_args(['analyze', '--run-id', 'fixture'])
            with patch.object(master, 'ROOT', root), \
                 patch.object(master.subprocess, 'run', side_effect=RuntimeError('broken raw evidence')), \
                 self.assertRaisesRegex(RuntimeError, 'broken raw evidence'):
                master.run_main(cli, analyze)
            self.assertEqual(previous.read_text(), 'previous successful batch\n')
            self.assertEqual(json.loads(manifest.read_text())['analysis_status'], 'failed')

    def test_five_experiments_share_one_folder_and_reruns_replace_only_their_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = {'experiment1.csv': 18, 'experiment2.csv': 15, 'experiment3.csv': 6,
                        'experiment4-wire.csv': 70, 'experiment4-wireless.csv': 70}
            for exp, link in [('1', 'wired'), ('2', 'wired'), ('3', 'wired'), ('4', 'wired'), ('4', 'wireless')]:
                fixture = self.fixture(directory, exp, link, f'exp{exp}-{link}')
                export_results(*fixture)
            paths = sorted(Path(directory).glob('*.csv'))
            self.assertEqual({path.name: len(self.read(path)) for path in paths}, expected)
            original = {path.name: path.read_bytes() for path in paths}
            fixture = self.fixture(directory, '4', 'wired', 'new-wired-batch')
            export_results(*fixture)
            for path in paths:
                if path.name == 'experiment4-wire.csv':
                    self.assertEqual({r['run_id'] for r in self.read(path)}, {'new-wired-batch'})
                else:
                    self.assertEqual(path.read_bytes(), original[path.name])

    def test_combined_runner_exports_separate_experiment_schemas(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = []
            for exp in ('1', '2', '3', '4'):
                config, args, dest, batch = self.fixture(directory, exp, run_id='combined')
                jobs.extend(batch)
            args.experiment = 'all'
            targets = export_results(config, args, dest, jobs)
            self.assertEqual(set(targets), {'1', '2', '3', '4'})
            self.assertNotIn('rx_mbps_mean', self.read(targets['1'])[0])
            self.assertNotIn('spdp_mean', self.read(targets['4'])[0])

    def test_fixed_wait_accounting_includes_both_qos_and_all_repetitions(self):
        args = master.parser().parse_args(['run', 'all', '--run-id', 'fixture'])
        wired = time_budget(args)
        self.assertEqual([r['trials'] for r in wired['experiments']], [90, 150, 60, 700])
        args.experiment = '4'
        wireless = time_budget(args)
        self.assertEqual(wireless['fixed_seconds'], 53270)
        self.assertEqual(wired['fixed_seconds'] + wireless['fixed_seconds'], 130315)


if __name__ == '__main__':
    unittest.main()
