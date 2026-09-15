#!/usr/bin/env python3
"""Decode recorded sequence IDs after measurement; do not compute statistics."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from common.metrics import fidelity_subscriber, require_complete_runs, require_run_metadata
from common.check_measurements import check
from common.sequence_data import write_sequences
from analyze import extract_bag_seqs, find_bag


def export(results, bags):
    require_complete_runs(results)
    count = 0
    for run in sorted(Path(results).glob('loss*/*/run*')):
        require_run_metadata(run)
        check(run)
        _, expected = fidelity_subscriber(run / 'sub.log')
        bag = find_bag(Path(bags), run.parent.parent.name, run.parent.name, run.name)
        sequences = extract_bag_seqs(bag)
        files = [bag] if bag.is_file() else sorted(bag.rglob('*'))
        source = dict(bag=str(bag), files=[dict(path=str(path), size_bytes=path.stat().st_size)
                                         for path in files if path.is_file()])
        write_sequences(run, sequences, expected, source)
        count += 1
    if not count:
        raise ValueError(f'No fidelity runs to export: {results}')
    print(f'[export] {count} recordings decoded to sequence CSVs; statistics run on master')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results-dir', type=Path, required=True)
    parser.add_argument('--bags-dir', type=Path, required=True)
    args = parser.parse_args()
    export(args.results_dir, args.bags_dir)
