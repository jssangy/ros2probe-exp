"""Discovery plotting entry points share the paper report and its validity rules."""

import argparse
from pathlib import Path


def configure():
    root = Path(__file__).resolve().parents[4]
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--results-dir', type=Path,
                        help='Directory containing a generated runs.csv for one observation host')
    parser.add_argument('--output-dir', type=Path,
                        default=root / 'results/01_discovery/analysis/paper')
    inputs.add_argument('--interhost-csv', type=Path,
                        help='Explicit detailed run CSV for one observation host')
    args = parser.parse_args()
    runs = args.interhost_csv if args.interhost_csv is not None else args.results_dir / 'runs.csv'
    if args.output_dir.resolve() == runs.parent.resolve():
        parser.error('Output must be separate from the input CSV directory')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    return args


def main():
    import sys
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from scripts.report_results import build_report
    args = configure()
    runs = args.interhost_csv if args.interhost_csv is not None else args.results_dir / 'runs.csv'
    build_report(1, runs, args.output_dir)
