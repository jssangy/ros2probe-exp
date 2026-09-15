"""Discovery ends when every workload process on both slaves reports matching."""
import csv
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from common.metrics import require_run_metadata

PROTOCOL = 'discovery-matching-v1'


def timestamp(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f'Missing/invalid discovery timestamp: {value!r}') from exc
    if not result.is_finite():
        raise ValueError(f'Nonfinite discovery timestamp: {value!r}')
    return result


def local_completion(folder, np, total, start, deadline):
    """Read all first-match records; never substitute a deadline for a missing match."""
    folder = Path(folder)
    start, deadline = timestamp(start), timestamp(deadline)
    expected = {f'node_{i}_topic_endpoint_count.csv' for i in range(np)}
    if {p.name for p in folder.glob('node_*_topic_endpoint_count.csv')} != expected:
        raise ValueError(f'Missing/extra participant matching reports: {folder}')
    times = []
    for i in range(np):
        with (folder / f'node_{i}_topic_endpoint_count.csv').open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 1:
            raise ValueError(f'Expected one first-match timestamp for participant {i}: {folder}')
        row = rows[0]
        try:
            counts = [int(row[k]) for k in ('stress_topic_0_pub', 'stress_topic_0_sub', 'total_pub', 'total_sub')]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f'Missing/invalid endpoint counts: {folder}') from exc
        if counts != [total] * 4:
            raise ValueError(f'Unexpected endpoint counts for participant {i}: {folder}')
        completion = timestamp(row.get('time_epoch'))
        if not start < completion < deadline:
            raise ValueError(f'Matching timestamp outside capture lifetime: {folder}')
        log_path = folder / f'participant_{i}.log'
        log = log_path.read_text() if log_path.is_file() else ''
        matches = re.findall(r'\[Step 3\] Matched: completion epoch=(\d+\.\d+)', log)
        if len(matches) != 1 or timestamp(matches[0]) != completion:
            raise ValueError(f'Inconsistent/missing matching log for participant {i}: {folder}')
        times.append(completion)
    return times


def read_run(folder):
    folder = Path(folder)
    protocol = require_run_metadata(folder, ('paper-v2', PROTOCOL))
    path = folder / 'run_metadata.json'
    if not path.is_file():
        raise ValueError(f'Missing measurement metadata: {path}')
    meta = json.loads(path.read_text(), parse_float=Decimal)
    if meta.get('protocol') != protocol:
        raise ValueError(f'Inconsistent discovery protocol metadata: {folder}')
    start = timestamp(meta.get('measure_start'))
    # Older raw captures explicitly stored their fixed deadline as measure_end.
    deadline = timestamp(meta.get('measure_end') if protocol == 'paper-v2' else meta.get('capture_deadline'))
    np, total = meta.get('N_P'), meta.get('total_processes')
    if np not in (1, 3, 5) or total != 2*np or meta.get('N_E') != 2:
        raise ValueError(f'Invalid two-slave Discovery participant configuration: {folder}')
    if meta.get('matched_processes') != np or deadline <= start:
        raise ValueError(f'Incomplete discovery matching metadata: {folder}')
    times = local_completion(folder, np, total, start, deadline)
    if protocol == PROTOCOL and timestamp(meta.get('local_discovery_complete')) != max(times):
        raise ValueError(f'Inconsistent local discovery completion: {folder}')
    return meta, start, deadline, times


def paired_window(folder, peer_folder):
    if Path(folder).resolve() == Path(peer_folder).resolve():
        raise ValueError('Discovery requires matching evidence from two distinct slaves')
    a, start, deadline_a, times_a = read_run(folder)
    b, peer_start, deadline_b, times_b = read_run(peer_folder)
    for key in ('protocol', 'N_P', 'N_E', 'total_processes', 'condition', 'ros_domain_id'):
        if a.get(key) != b.get(key):
            raise ValueError(f'Different slave trial metadata ({key}): {folder}, {peer_folder}')
    if start != peer_start or not a.get('host') or not b.get('host') or a['host'] == b['host']:
        raise ValueError('Discovery requires two hosts with the same scheduled start')
    end = max(*times_a, *times_b)
    if end >= min(deadline_a, deadline_b):
        raise ValueError('A capture lifetime does not cover global discovery completion')
    return dict(protocol=PROTOCOL, capture_protocol=a['protocol'], measure_start=str(start),
                measure_end=str(end), discovery_duration_ms=str((end-start)*1000),
                measurement_end_basis='all_workload_participants_matched',
                local_discovery_complete=str(max(times_a)), peer_discovery_complete=str(max(times_b)),
                local_participant_completion=[str(t) for t in times_a],
                peer_participant_completion=[str(t) for t in times_b])
