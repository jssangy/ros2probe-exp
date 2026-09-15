"""Write one CSV of paper metrics for a completed master experiment batch."""
from collections import defaultdict
import csv
from itertools import product
from pathlib import Path
import re

try:
    from .report_results import number, prepare, summary
except ImportError:
    from report_results import number, prepare, summary


def expected_groups(experiment, args):
    if experiment == 1:
        return list(product(args.np, ('baseline', 'rp_run', 'ros2_daemon')))
    if experiment == 2:
        return list(product(('pc',), args.resource_scenarios,
                           ('baseline', 'rp_hz', 'topic_hz', 'rp_bag', 'rosbag2')))
    if experiment == 3:
        return list(product(args.losses, ('rp_bag', 'rosbag2')))
    return list(product(args.probe_scenarios, ('baseline', 'rp_hz', 'topic_hz', 'rp_bag', 'rosbag2')))


def summarize_job(config, args, job):
    experiment = int(job['experiment'])
    with Path(job['runs_csv']).open(newline='') as stream:
        raw = list(csv.DictReader(stream))
    rows, excluded = prepare(experiment, raw)
    if excluded:
        raise ValueError(f'{job["job_id"]}: {len(excluded)} invalid/excluded rows; {excluded[0]["reason"]}')
    keys, metrics = {
        1: (['N_P', 'condition'], {'spdp_count': 'spdp', 'sedp_count': 'sedp',
                                 'control_count': 'control', 'discovery_messages': 'discovery_messages',
                                 'discovery_packet_count': 'discovery_packet_count',
                                 'discovery_duration_ms': 'discovery_duration_ms'}),
        2: (['platform', 'scenario', 'condition'], {'observer_cpu_norm_avg': 'cpu_pct',
                                                 'pss_mib': 'pss_mib', 'drop_pct': 'subscriber_drop_pct'}),
        3: (['loss_pct', 'condition'], {'actual_drop_pct': 'subscriber_drop_pct',
                                      'observer_drop_pct': 'recorder_drop_pct', 'lost_recall': 'loss_recall',
                                      'observer_extra_recv_count': 'extra_received',
                                      'observer_missed_recv_count': 'missed_received'}),
        4: (['scenario', 'condition'], {'rx_mbps': 'rx_mbps', 'drop_pct': 'subscriber_drop_pct'}),
    }[experiment]
    groups = defaultdict(list)
    for row in rows:
        if experiment == 4 and (row.get('link'), row.get('qos')) != (config['link'], job['qos']):
            raise ValueError(f'{job["job_id"]}: unexpected link/QoS in measurement')
        # These diagnostics must also have evidence; only baseline resource cost
        # and a loss-free trial's undefined recall may be missing.
        for metric in metrics:
            optional = ((experiment == 2 and row['condition'] == 'baseline' and metric != 'drop_pct') or
                        (experiment == 3 and metric == 'lost_recall' and number(row['actual_drop_pct']) == 0))
            if not optional and number(row.get(metric)) is None:
                raise ValueError(f'{job["job_id"]}: missing/nonfinite {metric}')
        groups[tuple(row[key] for key in keys)].append(row)
    group_order = {key: index for index, key in enumerate(expected_groups(experiment, args))}
    expected = set(group_order)
    if set(groups) != expected:
        raise ValueError(f'{job["job_id"]}: incomplete or unexpected conditions; '
                         f'missing={sorted(expected - set(groups))}, extra={sorted(set(groups) - expected)}')
    for key, group in groups.items():
        repetitions = []
        for row in group:
            value = str(row['rep' if experiment == 1 else 'run'])
            match = re.fullmatch(r'(?:run_?)?(\d+)', value)
            if not match:
                raise ValueError(f'{job["job_id"]}: invalid repetition {value}')
            repetitions.append(int(match[1]))
        if len(repetitions) != args.runs or set(repetitions) != set(range(1, args.runs + 1)):
            raise ValueError(f'{job["job_id"]}: incomplete/duplicate repetitions for {key}: {repetitions}')
    output = []
    statistics = summary(rows, keys, metrics)
    statistics.sort(key=lambda row: group_order[tuple(row[key] for key in keys)])
    for stats in statistics:
        row = {}
        if experiment == 1:
            row['slave'] = job['role']
        if experiment == 4:
            row.update(link=config['link'], qos=job['qos'])
        for key in keys:
            row['np_per_slave' if key == 'N_P' else 'injected_loss_pct' if key == 'loss_pct' else key] = stats[key]
        row['n_runs'] = stats['n_runs']
        for metric, name in metrics.items():
            row[name + '_mean'] = stats[metric + '_mean']
            row[name + '_sd'] = stats[metric + '_sd']
            if metric == 'lost_recall':
                row['loss_recall_n'] = stats[metric + '_n']
        output.append(row)
    return output


def result_paths(config, args, destination):
    """Stable master-side filenames, one for each experiment/link."""
    root = Path(destination).parent
    experiments = ('1', '2', '3', '4') if args.experiment == 'all' else (args.experiment,)
    link = {'wired': 'wire', 'wireless': 'wireless'}[config['link']]
    return {exp: root / (f'experiment4-{link}.csv' if exp == '4' else f'experiment{exp}.csv')
            for exp in experiments}


def export_results(config, args, destination, jobs):
    """Replace an experiment's latest CSV only after the whole batch validates."""
    targets = result_paths(config, args, destination)
    output = defaultdict(list)
    for job in jobs:
        rows = summarize_job(config, args, job)
        output[job['experiment']].extend(dict(run_id=args.run_id, **row) for row in rows)
    if set(output) != set(targets) or any(not rows for rows in output.values()):
        raise ValueError('Missing experiment measurements to export')
    pending = []
    try:
        # Prepare every file before replacing existing successful results.
        for experiment, target in targets.items():
            rows = output[experiment]
            fields = list(dict.fromkeys(key for row in rows for key in row))
            temporary = target.with_name(f'.{target.name}.{args.run_id}.tmp')
            pending.append((temporary, target))
            with temporary.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows({key: 'N/A' if value is None else value for key, value in row.items()}
                                 for row in rows)
        for temporary, target in pending:
            temporary.replace(target)
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
    return targets
