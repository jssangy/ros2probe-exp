"""A failed slave condition must stop the actual unattended batch shell."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProbeBatch(unittest.TestCase):
    def test_failed_first_condition_prevents_later_conditions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'experiments/04_probe_effect/scripts'
            scripts.mkdir(parents=True)
            shutil.copy2(ROOT / 'experiments/04_probe_effect/scripts/run_subscribers.sh', scripts)
            (scripts / 'run_once.sh').write_text('printf "%s\\n" "$2" >> attempted.txt\nexit 7\n')
            common = root / 'common'
            common.mkdir()
            (common / 'environment.sh').write_text('rp_exp_load_environment() { :; }\n')
            (common / 'perf_setup.sh').write_text('setup_platform_performance() { :; }\n')
            bindir = root / 'bin'
            bindir.mkdir()
            for name in ('sudo', 'nc'):
                path = bindir / name
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(0o755)
            process = subprocess.run(['/bin/bash', str(scripts / 'run_subscribers.sh'),
                                      '--sync', '127.0.0.1', '--scenarios', 'S1',
                                      '--conditions', 'baseline,rp_hz', '--runs', '1',
                                      '--nic', 'lo', '--platform', 'pc'], cwd=root,
                                     env=dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                                              RP_EXP_UNATTENDED='1'),
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 7, process.stdout + process.stderr)
            self.assertEqual((root / 'attempted.txt').read_text().splitlines(), ['baseline'])
            self.assertIn('aborting master-controlled batch', process.stdout)


if __name__ == '__main__':
    unittest.main()
