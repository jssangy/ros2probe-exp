"""Portable observation sequences: decoding on the slave, statistics on master."""
import csv
import hashlib
import json
from pathlib import Path


def write_sequences(folder, sequences, expected, source):
    folder = Path(folder)
    if not sequences or min(sequences) < 1 or max(sequences) > expected:
        raise ValueError(f'Empty/out-of-range recorder sequence data: {folder}')
    path = folder / 'observer_sequences.csv'
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['sequence'])
        writer.writerows([seq] for seq in sorted(sequences))
    temporary.replace(path)
    metadata = dict(format='ros2probe-sequences-v1', topic='/drop_image',
                    expected=expected, count=len(sequences), source=source,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    (folder / 'observer_sequences.json').write_text(json.dumps(metadata, indent=2) + '\n')


def read_sequences(folder, expected):
    folder = Path(folder)
    path = folder / 'observer_sequences.csv'
    metadata = json.loads((folder / 'observer_sequences.json').read_text())
    if (metadata.get('format') != 'ros2probe-sequences-v1' or metadata.get('topic') != '/drop_image'
            or metadata.get('expected') != expected
            or metadata.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError(f'Inconsistent sequence export metadata/hash: {folder}')
    with path.open(newline='') as stream:
        rows = csv.DictReader(stream)
        if rows.fieldnames != ['sequence']:
            raise ValueError(f'Unexpected sequence CSV columns: {path}')
        values = [int(row['sequence']) for row in rows]
    sequences = set(values)
    if (not sequences or len(values) != len(sequences) or len(values) != metadata.get('count')
            or min(sequences) < 1 or max(sequences) > expected):
        raise ValueError(f'Invalid exported sequence data: {path}')
    return sequences
