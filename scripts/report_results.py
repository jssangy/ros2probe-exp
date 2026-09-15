#!/usr/bin/env python3
"""Create separate paper-oriented CSV/Markdown/LaTeX tables and PDF/PNG figures.

Statistics are recomputed from detailed run rows. Summary-only repetition counts
are never used. Missing values remain missing, and one run has no estimated SD.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[1]
LABELS = {'baseline': 'Baseline', 'rp_run': 'rp run', 'ros2_daemon': 'ros2 daemon',
          'rp_hz': 'rp hz', 'topic_hz': 'ros2 hz', 'rp_bag': 'rp bag', 'rosbag2': 'rosbag2'}
COLORS = {'baseline': '#697586', 'rp_run': '#007C91', 'ros2_daemon': '#D46332',
          'rp_hz': '#007C91', 'topic_hz': '#D46332', 'rp_bag': '#46AFA3', 'rosbag2': '#9F4029'}
EXPERIMENTS = {1: 'Discovery transparency', 2: 'Resource overhead',
               3: 'Observation fidelity', 4: 'Probe effect'}
DISCOVERY_NP = [1, 3, 5]
DISCOVERY_REFERENCE = ROOT / 'experiments/01_discovery/reference/paper_figure.csv'


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def slug(value):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', str(value)).strip('._') or 'unnamed'


def write_csv(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fields:
        return
    with path.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def latex(value):
    return ''.join({'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
                    '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}'}.get(c, c) for c in str(value))


def summary(rows, keys, metrics):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(key, '') for key in keys)].append(row)
    result = []
    for key, group in sorted(groups.items()):
        out = dict(zip(keys, key))
        out['n_runs'] = len(group)
        for metric in metrics:
            values = [v for row in group if (v := number(row.get(metric))) is not None]
            out[metric + '_n'] = len(values)
            out[metric + '_mean'] = mean(values) if values else None
            out[metric + '_sd'] = stdev(values) if len(values) > 1 else None
        result.append(out)
    return result


def table(folder, name, rows, keys, metrics):
    write_csv(folder / (name + '.csv'), rows)
    headers = [key.replace('_', ' ') for key in keys] + ['Runs'] + list(metrics.values())
    records = []
    for row in rows:
        values = [LABELS.get(str(row[k]), str(row[k])) for k in keys] + [str(row['n_runs'])]
        for metric in metrics:
            avg, sd = row[metric + '_mean'], row[metric + '_sd']
            value = 'N/A' if avg is None else f'{avg:.3f}'
            if sd is not None:
                value += f' ± {sd:.3f}'
            if row[metric + '_n'] != row['n_runs']:
                value += f" (n={row[metric + '_n']})"
            values.append(value)
        records.append(values)
    note = 'Values are run means ± sample SD. SD is unavailable for one run; N/A is not zero.'
    md = '| ' + ' | '.join(headers) + ' |\n| ' + ' | '.join(['---'] * len(headers)) + ' |\n'
    md += '\n'.join('| ' + ' | '.join(row) + ' |' for row in records)
    (folder / (name + '.md')).write_text(md + '\n\n' + note + '\n')
    tex = '% ' + note + '\n\\begin{tabular}{' + 'l' * len(headers) + '}\n\\hline\n'
    tex += ' & '.join(map(latex, headers)) + r' \\' + '\n\\hline\n'
    tex += '\n'.join(' & '.join(latex(v).replace('±', r'$\pm$') for v in row) + r' \\' for row in records)
    tex += '\n\\hline\n\\end{tabular}\n'
    (folder / (name + '.tex')).write_text(tex)


def discovery_comparison(folder, name, stats):
    """Compare measured means with separately transcribed, rounded figure labels."""
    with DISCOVERY_REFERENCE.open(newline='') as stream:
        references = list(csv.DictReader(stream))
    lookup = {(int(row['N_P']), row['condition']): row for row in stats}
    records, display = [], []
    for reference in references:
        np, condition = int(reference['N_P']), reference['condition']
        measured = lookup.get((np, condition), {})
        for metric in ('spdp_count', 'sedp_count', 'control_count', 'discovery_messages'):
            published = float(reference[metric])
            avg = measured.get(metric + '_mean')
            delta = None if avg is None else 100 * (avg / published - 1)
            records.append({'N_P': np, 'condition': condition, 'metric': metric,
                            'paper_mean_rounded': published,
                            'measured_n': measured.get(metric + '_n', 0),
                            'measured_mean': avg, 'measured_sd': measured.get(metric + '_sd'),
                            'difference_pct': delta})
            if metric == 'discovery_messages':
                display.append([str(np), LABELS[condition], f'{published:.1f}',
                                str(measured.get(metric + '_n', 0)),
                                'N/A' if avg is None else f'{avg:.1f}',
                                'N/A' if delta is None else f'{delta:+.1f}%'])
    write_csv(folder / (name + '.csv'), records)
    headers = ['NP', 'Condition', 'Paper total', 'Measured n', 'Measured total', 'Difference']
    note = ('Totals are SPDP + SEDP + Control. Paper values are rounded figure labels, '
            'not raw runs. Differences alone do not identify the cause or establish reproducibility.')
    md = '| ' + ' | '.join(headers) + ' |\n| ' + ' | '.join(['---'] * len(headers)) + ' |\n'
    md += '\n'.join('| ' + ' | '.join(row) + ' |' for row in display)
    (folder / (name + '.md')).write_text(md + '\n\n' + note + '\n')
    tex = '% ' + note + '\n\\begin{tabular}{llrrrr}\n\\hline\n'
    tex += ' & '.join(map(latex, headers)) + r' \\' + '\n\\hline\n'
    tex += '\n'.join(' & '.join(map(latex, row)) + r' \\' for row in display)
    (folder / (name + '.tex')).write_text(tex + '\n\\hline\n\\end{tabular}\n')


def plot(folder, name, stats, axis, categories, conditions, metric, ylabel, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                         'pdf.fonttype': 42, 'svg.fonttype': 'none'})
    fig, ax = plt.subplots(figsize=(max(5.2, len(categories) * 1.4), 3.9))
    width = .76 / max(1, len(conditions))
    points = np.arange(len(categories))
    lookup = {(str(row[axis]), row['condition']): row for row in stats}
    heights = []
    for j, condition in enumerate(conditions):
        positions = points - .38 + width * (j + .5)
        avgs, errors, ns = [], [], []
        for category in categories:
            row = lookup.get((str(category), condition), {})
            avg = row.get(metric + '_mean')
            avgs.append(float('nan') if avg is None else avg)
            errors.append(row.get(metric + '_sd') or 0)
            ns.append(row.get(metric + '_n', 0))
        ax.bar(positions, avgs, width*.93, color=COLORS[condition], label=LABELS[condition],
               edgecolor='white', linewidth=.5)
        for x, avg, err, n in zip(positions, avgs, errors, ns):
            if n > 1 and math.isfinite(avg):
                ax.errorbar(x, avg, yerr=err, capsize=2.5, color='#17212B', linewidth=1, fmt='none')
        heights += [v + e for v, e in zip(avgs, errors) if math.isfinite(v)]
        for x, avg, err, n in zip(positions, avgs, errors, ns):
            if math.isfinite(avg):
                ax.annotate(f'n={n}', (x, avg + err), xytext=(0, 4), textcoords='offset points',
                            ha='center', va='bottom', fontsize=7)
            else:
                ax.annotate('N/A', (x, 0), xytext=(0, 3), textcoords='offset points',
                            ha='center', fontsize=7, rotation=90)
    if heights:
        ax.set_ylim(0, max(max(heights)*1.25, 1))
    else:
        ax.set_ylim(0, 1)
    if metric == 'lost_recall':
        ax.set_ylim(0, 1.15)
        ax.set_yticks([0, .2, .4, .6, .8, 1])
    ax.set_xticks(points, [str(c) for c in categories])
    # NaN bars do not contribute to autoscaling. Keep missing edge categories visible.
    ax.set_xlim(-.5, len(categories) - .5)
    ax.set_xlabel({'N_P': 'Participants per host (NP)', 'scenario': 'Workload',
                   'loss_pct': 'Injected packet loss (%)', 'metric': ''}.get(axis, axis))
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11, pad=32)
    ax.grid(axis='y', alpha=.2)
    ax.set_axisbelow(True)
    ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.01), ncol=min(5, len(conditions)),
              frameon=False, fontsize=8)
    fig.tight_layout()
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ('png', 'pdf'):
        fig.savefig(folder / f'{name}.{suffix}', dpi=180, bbox_inches='tight')
    plt.close(fig)


def prepare(experiment, raw):
    rows, excluded, identities = [], [], set()
    all_bag = {(r.get('scenario'), c) for r in raw for c in ('rp_bag', 'rosbag2')
               if r.get('condition') == c + '_all'}
    for index, original in enumerate(raw, 2):
        row = dict(original)
        reason = None
        if str(row.get('validity', '')).startswith(('invalid', 'suspect_low_')):
            reason = 'measurement validity: ' + row['validity']
        if experiment == 4:
            condition = row.get('condition', '')
            for kind in ('rp_bag', 'rosbag2'):
                if condition in (kind + '_one', kind + '_all'):
                    if condition.endswith('_one') and (row.get('scenario') == 'S7' or
                                                       (row.get('scenario'), kind) in all_bag):
                        reason = 'single-topic recording outside selected paper matrix'
                    row['condition'] = kind
            keys = ('scenario', 'condition', 'run')
            row['drop_pct'] = number(row.get('drop_pct'))
        elif experiment == 2:
            keys = ('platform', 'scenario', 'condition', 'run')
            value = number(row.get('observer_mem_avg_kb'))
            row['pss_mib'] = None if value is None else value / 1024
        elif experiment == 3:
            keys = ('loss_pct', 'condition', 'run')
            row['loss_pct'] = int(row['loss_pct'])
        else:
            keys = ('root', 'N_P', 'condition', 'rep')
            row['N_P'] = int(row['N_P'])
            if row['N_P'] not in DISCOVERY_NP:
                reason = 'NP outside paper matrix (1, 3, 5)'
            if row.get('scale') and row['scale'] != f"G{row['N_P']}":
                reason = 'scale label does not match N_P'
            components = [number(row.get(k)) for k in ('spdp_count', 'sedp_count', 'control_count')]
            row['discovery_messages'] = sum(components) if all(v is not None for v in components) else None
        required = {1: ['spdp_count', 'sedp_count', 'control_count', 'discovery_messages'],
                    2: ['drop_pct'], 3: ['actual_drop_pct', 'observer_drop_pct'],
                    4: ['rx_mbps', 'drop_pct']}[experiment]
        if experiment == 2 and row.get('condition') != 'baseline':
            required += ['observer_cpu_norm_avg', 'pss_mib']
        if experiment == 3 and (number(row.get('actual_drop_pct')) or 0) > 0:
            required += ['lost_recall']
        missing = [key for key in required if number(row.get(key)) is None]
        if missing:
            reason = 'missing/nonfinite measured values: ' + ', '.join(missing)
        if row.get('condition') not in LABELS:
            reason = 'condition outside the paper comparison'
        if reason:
            excluded.append({'csv_line': index, 'reason': reason, 'identity': '/'.join(str(row.get(k, '')) for k in keys)})
            continue
        identity = tuple(str(row.get(key, '')) for key in keys)
        if identity in identities:
            raise ValueError(f'Duplicate run identity; do not pool result sets: {identity}')
        identities.add(identity)
        rows.append(row)
    # Different measurement protocols must not silently form one error bar.
    protocols = {r.get('protocol') or 'unverified' for r in rows}
    if len(protocols) > 1:
        raise ValueError(f'Mixed measurement protocols in one input: {sorted(protocols)}')
    execution_profiles = {r.get('execution_profile') or 'unspecified' for r in rows}
    if len(execution_profiles) > 1:
        raise ValueError(f'Mixed execution profiles in one input: {sorted(execution_profiles)}')
    durations = {round((float(r['measure_end_ms']) - float(r['measure_start_ms'])) / 1000, 3)
                 for r in rows if r.get('protocol') == 'paper-v2' and r.get('measure_start_ms') and r.get('measure_end_ms')}
    if len(durations) > 1:
        raise ValueError(f'Mixed measurement durations: {sorted(durations)}')
    profiles = {(r['link'], r['qos']) for r in rows if r.get('link') and r.get('qos')}
    if len(profiles) > 1:
        raise ValueError(f'Mixed link/QoS profiles: {sorted(profiles)}')
    return rows, excluded


def build_report(experiment, runs_path, output_dir, profile='', expected_runs=10, require_complete=False):
    runs_path, output_dir = Path(runs_path), Path(output_dir)
    with runs_path.open(newline='') as stream:
        raw = list(csv.DictReader(stream))
    rows, excluded = prepare(experiment, raw)
    if not rows:
        raise ValueError('No eligible detailed run rows for a report')
    # Never overwrite raw inputs or any retained CSV with derived reports.
    if output_dir.resolve() == runs_path.parent.resolve():
        raise ValueError('Report output must be a separate directory from the input CSV')
    output_dir.mkdir(parents=True, exist_ok=True)
    tables, figures = output_dir/'tables', output_dir/'figures'
    tables.mkdir(exist_ok=True)
    coverage = []
    links = []

    def emit(name, group, keys, metrics, axis, conditions, categories=None, title='', show_missing=False):
        stats = summary(group, keys, metrics)
        if show_missing:
            lookup = {(row[axis], row['condition']): row for row in stats}
            stats = []
            for category in categories:
                for condition in conditions:
                    empty = {axis: category, 'condition': condition, 'n_runs': 0}
                    for metric in metrics:
                        empty.update({metric+'_n': 0, metric+'_mean': None, metric+'_sd': None})
                    stats.append(lookup.get((category, condition), empty))
        table(tables, name, stats, keys, metrics)
        links.append((name, title or name, list(metrics)))
        if categories is None:
            categories = sorted({r[axis] for r in group})
        for metric, label in metrics.items():
            plot(figures, name+'_'+metric, stats, axis, categories, conditions,
                 metric, label, title)
        return stats

    def cover(scope, categories, conditions, group, axis):
        for category in categories:
            for condition in conditions:
                n = sum(str(r[axis]) == str(category) and r['condition'] == condition for r in group)
                coverage.append({'scope': scope, axis: category, 'condition': condition,
                                 'n_runs': n, 'expected_runs': expected_runs,
                                 'status': 'complete' if n >= expected_runs else 'missing' if n == 0 else 'partial'})

    if experiment == 1:
        metrics = {'spdp_count': 'SPDP messages', 'sedp_count': 'SEDP messages',
                   'control_count': 'Control messages', 'discovery_messages': 'Discovery messages (total)'}
        for host in sorted({r['root'] for r in rows}):
            group = [r for r in rows if r['root'] == host]
            conditions = ['baseline', 'rp_run', 'ros2_daemon']
            name = 'discovery_' + slug(host)
            stats = emit(name, group, ['N_P', 'condition'], metrics, 'N_P', conditions,
                         DISCOVERY_NP, title=f'Experiment 1: {host}', show_missing=True)
            discovery_comparison(tables, name+'_paper_comparison', stats)
            links.append((name+'_paper_comparison', f'Published figure comparison: {host}', []))
            cover(host, DISCOVERY_NP, conditions, group, 'N_P')
        write_csv(output_dir/'input_totals_audit.csv', [
            {'root': r['root'], 'N_P': r['N_P'], 'condition': r['condition'], 'rep': r['rep'],
             'input_discovery_packets': r.get('discovery_packets'),
             'reported_discovery_messages': r['discovery_messages'],
             'totals_agree': number(r.get('discovery_packets')) == r['discovery_messages']}
            for r in rows])
    elif experiment == 2:
        for platform in ['pc', 'jetson', 'rpi']:
            for mode, conditions in [('rate', ['rp_hz', 'topic_hz']), ('recording', ['rp_bag', 'rosbag2'])]:
                group = [r for r in rows if r['platform'] == platform and r['condition'] in conditions]
                scenarios = ['ST100', 'ST500'] if platform == 'rpi' and mode == 'recording' else ['ST100', 'ST500', 'ST1000']
                cover(platform+'/'+mode, scenarios, conditions, group, 'scenario')
                if group:
                    emit(f'{mode}_{platform}', group, ['scenario', 'condition'],
                         {'observer_cpu_norm_avg': 'Observer CPU (% of total cores)', 'pss_mib': 'Observer PSS (MiB)'},
                         'scenario', conditions, scenarios, f'Experiment 2: {platform}, {mode}')
            baseline = [r for r in rows if r['platform'] == platform and r['condition'] == 'baseline']
            cover(platform+'/baseline', ['ST100', 'ST500', 'ST1000'], ['baseline'], baseline, 'scenario')
        table(tables, 'subscriber_health', summary(rows, ['platform', 'scenario', 'condition'], ['drop_pct']),
              ['platform', 'scenario', 'condition'], {'drop_pct': 'Subscriber drop (%)'})
    elif experiment == 3:
        conditions = ['rosbag2', 'rp_bag']
        metrics = {'actual_drop_pct': 'Subscriber drop (%)', 'observer_drop_pct': 'Observer drop (%)',
                   'lost_recall': 'Loss recall (fraction)'}
        emit('fidelity', rows, ['loss_pct', 'condition'], metrics, 'loss_pct', conditions,
             title='Experiment 3: observation fidelity')
        cover('fidelity', [0, 10, 20], conditions, rows, 'loss_pct')
    else:
        conditions = ['baseline', 'rp_hz', 'topic_hz', 'rp_bag', 'rosbag2']
        cover(profile or runs_path.parent.name, [f'S{i}' for i in range(1, 8)], conditions, rows, 'scenario')
        for scenario in sorted({r['scenario'] for r in rows}):
            group = [r for r in rows if r['scenario'] == scenario]
            emit(slug(profile+'_'+scenario), group, ['scenario', 'condition'],
                 {'rx_mbps': 'NIC received bandwidth (Mbps)', 'drop_pct': 'Original-subscriber drop (%)'},
                 'scenario', conditions, [scenario], f"Experiment 4: {profile.replace('_', ' ')} / {scenario}".strip())
    # Common keys make coverage machine-readable even across experiment schemas.
    fields = sorted({key for row in coverage for key in row})
    write_csv(output_dir/'coverage.csv', [{key: row.get(key, '') for key in fields} for row in coverage])
    write_csv(output_dir/'excluded_runs.csv', excluded, ['csv_line', 'reason', 'identity'])
    manifest = {'experiment': experiment, 'title': EXPERIMENTS[experiment], 'profile': profile,
                'input': str(runs_path.resolve()), 'sha256': hashlib.sha256(runs_path.read_bytes()).hexdigest(),
                'raw_run_rows': len(raw), 'included_run_rows': len(rows), 'excluded_run_rows': len(excluded),
                'quality_flags': sorted({r['validity'] for r in rows if r.get('validity') and r['validity'] != 'valid'}),
                'expected_repetitions': expected_runs, 'complete': all(r['status']=='complete' for r in coverage),
                'protocols': sorted({r.get('protocol') or 'unverified' for r in rows}),
                'statistics': 'Mean and sample standard deviation across independent runs; SD undefined at n=1.',
                'discovery_observation_unit': 'Each host is summarized separately; two hosts are not two repetitions.',
                'fidelity_zero_loss': 'Recall is undefined when the subscriber lost no messages.',
                'probe_S7_loss': 'Primary /points/front subscriber; other topics remain separate in topic_results.csv.'}
    if experiment == 1:
        manifest['paper_NP'] = DISCOVERY_NP
        manifest['measurement_end_bases'] = sorted({r.get('measurement_end_basis', 'unspecified') for r in rows})
        manifest['discovery_total_definition'] = 'Per-run SPDP + SEDP + Control; input discovery_packets is audited separately.'
        manifest['input_total_mismatches'] = sum(number(r.get('discovery_packets')) != r['discovery_messages'] for r in rows)
        manifest['paper_reference'] = {'file': str(DISCOVERY_REFERENCE.relative_to(ROOT)),
                                     'sha256': hashlib.sha256(DISCOVERY_REFERENCE.read_bytes()).hexdigest(),
                                     'kind': 'rounded published figure labels, not measured runs'}
    (output_dir/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    index = f'# Experiment {experiment}: {EXPERIMENTS[experiment]}\n\n'
    index += f'{len(rows)} detailed rows included; {len(excluded)} excluded. '
    index += ('The supplied comparison has the expected run coverage.' if manifest['complete'] else
              'This is a partial result set; see [coverage](coverage.csv) for missing conditions or repetitions.')
    index += '\n\nMeans and sample SD are computed from detailed rows. N/A is not zero; n=1 has no estimated SD. '
    index += 'These outputs summarize the supplied measurements and do not assert that the paper was reproduced.\n\n'
    index += '[Provenance](manifest.json) · [Excluded rows](excluded_runs.csv)\n\n'
    if experiment == 1:
        if manifest['measurement_end_bases'] == ['all_workload_participants_matched']:
            index += ('Counts cover [scheduled T, latest first-match timestamp across every workload '
                      'participant on both slaves). The capture deadline is not the counting endpoint. '
                      'Completion is sampled by the workload at approximately 1 ms intervals; '
                      'it does not mean all DDS control traffic has ceased.\n\n')
        index += ('Only NP=1, 3, 5 enter the paper comparison. Missing scales remain N/A. '
                  'Total messages are computed per run as SPDP + SEDP + Control. '
                  '[Input total audit](input_totals_audit.csv) preserves the supplied total column separately; '
                  'its counting unit is not assumed to match this sum.\n\n')
    for name, title, metrics in links:
        index += f'## {title}\n\n[CSV](tables/{name}.csv) · [Markdown](tables/{name}.md) · [LaTeX](tables/{name}.tex)\n\n'
        for metric in metrics:
            index += f'![{title}: {metric}](figures/{name}_{metric}.png)\n\n[PDF](figures/{name}_{metric}.pdf)\n\n'
    (output_dir/'README.md').write_text(index)
    print(f'[report] Experiment {experiment}: {output_dir}')
    if require_complete and not manifest['complete']:
        raise ValueError('Incomplete paper matrix; inspect coverage.csv')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=int, choices=EXPERIMENTS)
    parser.add_argument('--runs', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--profile', default='')
    parser.add_argument('--expected-runs', type=int, default=10)
    parser.add_argument('--require-complete', action='store_true')
    parser.add_argument('--retained', type=Path, help='Generate separate reports from a retained four-experiment CSV collection')
    args = parser.parse_args()
    if args.expected_runs < 1:
        parser.error('--expected-runs must be positive')
    if args.retained:
        for exp, name in [(1, '01_discovery'), (2, '02_resource_overhead'), (3, '03_observation_fidelity')]:
            build_report(exp, args.retained/name/'runs.csv', args.output_dir/name,
                         expected_runs=args.expected_runs, require_complete=args.require_complete)
        for profile in ['wired_best_effort', 'wired_reliable', 'wireless_best_effort', 'wireless_reliable']:
            build_report(4, args.retained/'04_probe_effect'/profile/'runs.csv',
                         args.output_dir/'04_probe_effect'/profile, profile, args.expected_runs, args.require_complete)
        (args.output_dir/'README.md').write_text('# ros2probe measurement reports\n\n'+
            '\n'.join(f'- [Experiment {exp}: {EXPERIMENTS[exp]}]({name}/README.md)' for exp, name in
                      [(1,'01_discovery'),(2,'02_resource_overhead'),(3,'03_observation_fidelity')])+ '\n'+
            '\n'.join(f'- [Experiment 4: {p}](04_probe_effect/{p}/README.md)' for p in
                      ['wired_best_effort','wired_reliable','wireless_best_effort','wireless_reliable'])+'\n')
    else:
        if not args.experiment or not args.runs:
            parser.error('--experiment and --runs are required unless --retained is used')
        build_report(args.experiment, args.runs, args.output_dir, args.profile,
                     args.expected_runs, args.require_complete)


if __name__ == '__main__':
    main()
