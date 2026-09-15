"""Discovery paper matrix and published-value comparison regressions."""
import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from scripts.report_results import prepare, summary, discovery_comparison

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'experiments/01_discovery/scripts'


class DiscoveryPaper(unittest.TestCase):
    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(SCRIPTS/name), *args],
                              capture_output=True, text=True, timeout=10)

    def test_matrix_has_only_three_scales_and_consistent_participant_totals(self):
        run = self.run_script('runall.py', '--scale', 'all', '--condition', 'baseline',
                              '--repetitions', '1', '--hosts', 'worker-a', 'worker-b',
                              '--capture-interface', 'eno1', '--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        for np in (1, 3, 5):
            self.assertIn(f'--N_P {np} --total-processes {2*np}', run.stdout)
        self.assertNotIn('G10', run.stdout)

    def test_invalid_scale_or_label_cannot_start_a_run(self):
        for arguments in [('--scale', 'G10', '--N_P', '10'),
                          ('--scale', 'G3', '--N_P', '5')]:
            with self.subTest(arguments=arguments):
                run = self.run_script('run_master_batch.py', *arguments,
                                      '--condition', 'baseline', '--rep', '1')
                self.assertNotEqual(run.returncode, 0)
                self.assertNotIn('[master]', run.stdout)
        run = self.run_script('master_runner.py', '2', '10', '--hosts', 'worker-a',
                              '--capture-interface', 'eno1', '--dry-run')
        self.assertNotEqual(run.returncode, 0)

    def test_report_excludes_np10_without_relabeling_and_uses_component_sum(self):
        raw = [dict(root='a', scale=f'G{np}', N_P=str(np), condition='baseline', rep='1',
                    spdp_count='6', sedp_count='38', control_count='8', discovery_packets='99')
               for np in (1, 5, 10)]
        rows, excluded = prepare(1, raw)
        self.assertEqual([r['N_P'] for r in rows], [1, 5])
        self.assertEqual(rows[0]['discovery_messages'], 52)
        self.assertEqual(rows[0]['discovery_packets'], '99')
        self.assertEqual(len(excluded), 1)
        self.assertIn('NP outside paper matrix', excluded[0]['reason'])
        self.assertEqual(raw[2]['N_P'], '10')

    def test_paper_reference_never_fills_missing_measurements(self):
        rows, _ = prepare(1, [dict(root='a', N_P='1', condition='baseline', rep='1',
                                  spdp_count='6', sedp_count='38', control_count='8')])
        stats = summary(rows, ['N_P', 'condition'],
                        ['spdp_count', 'sedp_count', 'control_count', 'discovery_messages'])
        with tempfile.TemporaryDirectory() as d:
            discovery_comparison(Path(d), 'comparison', stats)
            with (Path(d)/'comparison.csv').open() as stream:
                comparison = list(csv.DictReader(stream))
            missing = [r for r in comparison if r['N_P']=='3']
            self.assertEqual(len(missing), 12)
            self.assertTrue(all(r['measured_n']=='0' and r['measured_mean']=='' for r in missing))
            measured = next(r for r in comparison if r['N_P']=='1' and r['condition']=='baseline'
                            and r['metric']=='discovery_messages')
            self.assertEqual(float(measured['difference_pct']), 0)
            self.assertEqual(measured['measured_sd'], '')


if __name__ == '__main__':
    unittest.main()
