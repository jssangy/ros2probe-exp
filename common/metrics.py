"""Strict measurement-window parsing shared by result analyzers."""
from pathlib import Path
import re
import math


def interval(path):
    text = Path(path).read_text()
    start = re.search(r'MEASURE_START_MS\s+(\d+)', text)
    end = re.search(r'MEASURE_END_MS\s+(\d+)', text)
    if not start or not end or int(end[1]) <= int(start[1]):
        raise ValueError(f'Missing/invalid measurement interval: {path}')
    return int(start[1]), int(end[1])


def read_samples(path, columns=2):
    rows = []
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) < columns:
            raise ValueError(f'Malformed sample in {path}: {line}')
        row = [float(p) for p in parts[:columns]]
        if not all(math.isfinite(value) for value in row):
            raise ValueError(f'Nonfinite sample: {path}')
        if rows and row[0] <= rows[-1][0]:
            raise ValueError(f'Nonmonotonic timestamps: {path}')
        rows.append(row)
    if not rows:
        raise ValueError(f'No samples: {path}')
    return rows


def bandwidth(path, start, end):
    rows = read_samples(path)
    if len(rows) < 2 or rows[0][0] > start or rows[-1][0] < end:
        raise ValueError(f'NIC samples do not cover the measurement window: {path}')
    weighted_bits, duration_ms = 0.0, 0.0
    for (t0, b0), (t1, b1) in zip(rows, rows[1:]):
        if b1 < b0 or t1 - t0 > 2500:
            raise ValueError(f'NIC counter reset or sample gap >2.5s: {path}')
        overlap = max(0, min(end, t1) - max(start, t0))
        weighted_bits += (b1 - b0) * 8 * overlap / (t1 - t0)
        duration_ms += overlap
    if duration_ms != end - start:
        raise ValueError(f'Incomplete NIC interval: {path}')
    return len(rows), weighted_bits / (duration_ms / 1000) / 1e6, duration_ms / 1000, round(weighted_bits / 8)


def require_complete_runs(root):
    for status in Path(root).rglob('run_status.txt'):
        if status.read_text().strip() != 'complete':
            raise ValueError(f'Incomplete/failed run must be rerun before analysis: {status.parent}')


def require_run_metadata(folder, allowed_protocols=('paper-v2',)):
    folder = Path(folder)
    status = folder / 'run_status.txt'
    if not status.is_file() or status.read_text().strip() != 'complete':
        raise ValueError(f'Missing completion evidence or incomplete/failed run: {folder}')
    version = folder / 'protocol_version.txt'
    if not version.is_file() or version.read_text().strip() not in allowed_protocols:
        raise ValueError(f'Missing/unsupported measurement protocol: {folder}')
    return version.read_text().strip()


def rate_observer(path):
    values = re.findall(r'average rate:\s*(\S+)', Path(path).read_text())
    try:
        rates = [float(value) for value in values]
    except ValueError as error:
        raise ValueError(f'Malformed rate observer output: {path}') from error
    if not rates or not all(math.isfinite(rate) and rate > 0 for rate in rates):
        raise ValueError(f'Missing/invalid rate observer samples: {path}')


def fidelity_subscriber(path):
    path = Path(path)
    text = path.read_text() if path.is_file() else ''
    configs = re.findall(r'^CONFIG .*?\bexpected=(\d+)\b', text, re.M)
    seqs = {int(value) for value in re.findall(r'^SEQ\s+(\d+)\s*$', text, re.M)}
    if len(configs) != 1 or int(configs[0]) <= 0 or not seqs:
        raise ValueError(f'Missing subscriber sequence evidence: {path}')
    expected = int(configs[0])
    finals = re.findall(r'FINAL \[\d+s\]: recv (\d+) / expected (\d+)', text)
    if len(finals) != 1 or tuple(map(int, finals[0])) != (len(seqs), expected):
        raise ValueError(f'Inconsistent subscriber FINAL/sequence count: {path}')
    if min(seqs) < 1 or max(seqs) > expected:
        raise ValueError(f'Out-of-range subscriber sequence: {path}')
    return seqs, expected


def timed_subscribers(path, expected_count=1):
    """Validate each standalone or ros2-launch-prefixed subscriber independently."""
    text = Path(path).read_text()
    records = {}
    marker = re.compile(r'CONFIG topic=|MEASURE_START_MS |MEASURE_END_MS |TOPIC_RESULT |FINAL \[')
    for line in text.splitlines():
        match = marker.search(line)
        if match:
            records.setdefault(line[:match.start()], []).append(line[match.start():])
    if len(records) != expected_count:
        raise ValueError(f'Expected {expected_count} subscriber result(s): {path}')
    topics = set()
    for lines in records.values():
        record = '\n'.join(lines)
        config = re.findall(r'CONFIG topic=(\S+) hz=([\d.]+) warmup_sec=(\d+) measure_sec=(\d+) qos=(\S+)', record)
        starts = re.findall(r'MEASURE_START_MS (\d+)', record)
        ends = re.findall(r'MEASURE_END_MS (\d+)', record)
        final = re.findall(r'FINAL \[(\d+)s\]: recv (\d+) / expected (\d+)', record)
        result = re.findall(r'TOPIC_RESULT topic=(\S+) recv=(\d+) expected=(\d+) duration=(\d+)', record)
        if any(len(items) != 1 for items in (config, starts, ends, final, result)):
            raise ValueError(f'Missing/duplicate subscriber measurement evidence: {path}')
        topic, hz, _warmup, duration, _qos = config[0]
        expected = math.floor(float(hz) * int(duration) + 0.5)
        seconds, received, total = map(int, final[0])
        rt, rr, rexp, rd = result[0]
        if (expected <= 0 or seconds != int(duration) or total != expected
                or int(ends[0]) - int(starts[0]) != seconds * 1000
                or (rt, int(rr), int(rexp), int(rd)) != (topic, received, total, seconds)
                or topic in topics):
            raise ValueError(f'Inconsistent subscriber counts/duration/topic: {path}')
        topics.add(topic)
    return topics


def resource_usage(path, start, end, baseline=False):
    rows = read_samples(path, 4)
    if len(rows) < 2 or rows[0][0] > start or rows[-1][0] < end:
        raise ValueError(f'Resource samples do not cover the measurement window: {path}')
    cpu_integral, elapsed, cpus, memories = 0.0, 0.0, [], []
    for previous, current in zip(rows, rows[1:]):
        t0, t1 = previous[0], current[0]
        if t1 - t0 > 2500:
            raise ValueError(f'Resource sample gap >2.5s: {path}')
        overlap = max(0, min(end, t1) - max(start, t0))
        if overlap:
            if current[1] < 0 or (not baseline and (current[2] <= 0 or current[3] <= 0)):
                raise ValueError(f'Missing observer CPU/PSS/process evidence: {path}')
            cpu_integral += current[1] * overlap
            elapsed += overlap
            cpus.append(current[1])
        if start <= t1 <= end:
            memories.append(current[2])
    if elapsed != end - start or not memories:
        raise ValueError(f'Incomplete resource interval: {path}')
    return {'observer_samples': len(cpus), 'observer_cpu_avg': cpu_integral / elapsed,
            'observer_cpu_max': max(cpus), 'observer_mem_avg_kb': sum(memories) / len(memories),
            'observer_mem_max_kb': max(memories)}
