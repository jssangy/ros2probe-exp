"""Configuration and experiment plans for one master and exactly two slaves."""
import ipaddress
import json
from pathlib import Path
import re
import shlex
import os
import stat

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = {'1': '01_discovery', '2': '02_resource_overhead',
               '3': '03_observation_fidelity', '4': '04_probe_effect'}
ROLES = ('slave_a', 'slave_b')
PAPER_NP = [1, 3, 5]
RESOURCE_SCENARIOS = ['ST100', 'ST500', 'ST1000']
PROBE_SCENARIOS = [f'S{i}' for i in range(1, 8)]
LOSS_LEVELS = [0, 10, 20]
QOS_PROFILES = ('best_effort', 'reliable')


def qos_profiles(args):
    return QOS_PROFILES if args.qos == 'both' else (args.qos,)


def time_budget(args):
    """Known sequential waits, not an upper bound on end-to-end runtime.

    Discovery: scheduled epoch +30, capture deadline +7, cooldown 25.
    Exp2: scheduled START +5, warmup 10, measurement 60, last sample 1.1,
          cooldown 10. Exp3: observer lead 3, prestart 5, matched-reader settle 3,
          scheduled GO +5, publication 60 (subscriber timeout 62), cooldown 10.
    Exp4: scheduled START +5, measurement 60, last sample 1.1, cooldown 10.
    Startup, readiness, clock checks, shutdown, bag reads and collection vary.
    """
    selected = list(EXPERIMENTS) if args.experiment == 'all' else [args.experiment]
    rows = []
    for exp in selected:
        count, seconds = {
            '1': (len(args.np) * 3 * args.runs, 62),
            '2': (len(args.resource_scenarios) * 5 * args.runs, 86.1),
            '3': (len(args.losses) * 2 * args.runs, 88),
            '4': (len(args.probe_scenarios) * 5 * len(qos_profiles(args)) * args.runs, 76.1),
        }[exp]
        rows.append(dict(experiment=exp, trials=count, fixed_seconds_per_trial=seconds,
                         fixed_seconds=round(count * seconds, 1)))
    return dict(experiments=rows, fixed_seconds=round(sum(row['fixed_seconds'] for row in rows), 1),
                note='Fixed waits only; add readiness, clock checks, startup/shutdown, recording validation, export and collection. '
                     'Discovery epoch rounding and fidelity early completion can shorten the subtotal slightly; '
                     'there is no end-to-end completion guarantee.')


def label(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
        raise ValueError('Run IDs must contain only letters, digits, underscores or hyphens')
    return value


def validate(config):
    if set(config['hosts']) != set(ROLES):
        raise ValueError('Configure exactly slave_a and slave_b')
    if type(config.get('domain_id')) is not int or not 0 <= config['domain_id'] <= 232:
        raise ValueError('domain_id must be an integer from 0 to 232')
    if config.get('link') not in ('wired', 'wireless'):
        raise ValueError('link must be wired or wireless')
    clock = config.get('master_clock_address')
    if clock is not None:
        address = ipaddress.IPv4Address(clock)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError('master_clock_address must be reachable from both slaves')
    for host in config['hosts'].values():
        if host['repo'].rstrip('/') in ('', '~'):
            raise ValueError('repo must be a dedicated directory, not / or the home directory')
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+', host['ssh']) or host['ssh'].startswith('-'):
            raise ValueError('ssh must be a host alias or user@host, without SSH options')
        address = ipaddress.IPv4Address(host['address'])
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError('Use the slave data-link IPv4 address')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host['nic']) or host['nic'] == 'lo':
            raise ValueError('Use a non-loopback data NIC')
        for key in ('repo', 'ros_setup'):
            value = host[key]
            if not (value.startswith('/') or value.startswith('~/')) or any(c in value for c in '\n\r\0'):
                raise ValueError(f'{key} must be an absolute path or start with ~/')
    a, b = (config['hosts'][role] for role in ROLES)
    if a['ssh'] == b['ssh'] or a['address'] == b['address']:
        raise ValueError('The two slaves must be different machines with different data IPs')
    if clock in (a['address'], b['address']):
        raise ValueError('The master clock address must be separate from both slaves')
    if a['ros_setup'] != b['ros_setup']:
        raise ValueError('Use the same ROS setup path/distro on both slaves')
    return config


def default_config():
    local = ROOT / '.testbed.local.json'
    return local if local.exists() else ROOT / '.testbed.json'


def read_config(path):
    path = Path(path)
    document = json.loads(path.read_text())
    if 'slaves' in document:
        if document.get('version') != 1:
            raise ValueError('Single-file testbed version must be 1')
        if set(document['slaves']) != set(ROLES):
            raise ValueError('Configure exactly slave_a and slave_b')
        if document.get('ros_distro') not in ('humble', 'jazzy'):
            raise ValueError('ros_distro must be humble or jazzy')
        owners = [document.get('master', {}), *document['slaves'].values()]
        contains_password = False
        for owner in owners:
            for field in ('password', 'ssh_password', 'sudo_password'):
                value = owner.get(field, '')
                if not isinstance(value, str) or any(c in value for c in '\n\r\0'):
                    raise ValueError('Passwords must be single-line strings')
                contains_password |= bool(value)
        if contains_password:
            info = path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError('Password config must be owned by this user and private: chmod 600 ' + str(path))
        for link in ('wired', 'wireless'):
            public_config(document, link)
        for slave in document['slaves'].values():
            if slave['wired']['nic'] == slave['wireless']['nic'] or slave['wired']['ip'] == slave['wireless']['ip']:
                raise ValueError('Each slave needs distinct wired and wireless NICs/IPs')
    return document


def public_config(document, link=None):
    """Allowlist only execution fields; credentials never enter job/result JSON."""
    if 'slaves' in document:
        selected = link or document.get('default_link', 'wired')
        if selected not in ('wired', 'wireless'):
            raise ValueError('link must be wired or wireless')
        config = dict(master_clock_address=document['master']['clock_address'],
                      domain_id=document.get('domain_id', 77), link=selected, hosts={})
        for role in ROLES:
            slave = document['slaves'][role]
            user = slave['user']
            if not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
                raise ValueError('Use a Linux account name in each slave user field')
            interface = slave[selected]
            config['hosts'][role] = dict(ssh=user + '@' + interface['ip'],
                address=interface['ip'], nic=interface['nic'],
                repo=slave.get('repo', '~/ros2probe-exp'),
                ros_setup='/opt/ros/' + document['ros_distro'] + '/setup.bash',
                rp_bin=slave.get('rp_bin', ''))
    else:
        if link and link != document['link']:
            raise ValueError('Legacy config contains one link; select its file or use .testbed.local.json')
        config = {k:document[k] for k in ('domain_id', 'link', 'hosts')}
        if 'master_clock_address' in document:
            config['master_clock_address'] = document['master_clock_address']
        config['hosts'] = {role:{key:host[key] for key in
            ('ssh','address','nic','repo','ros_setup','rp_bin') if key in host}
            for role,host in document['hosts'].items()}
    return validate(config)


def load(path, link=None):
    return public_config(read_config(path), link)


def credentials(path):
    document = read_config(path)
    result = dict(master='', slaves={role:dict(ssh='', sudo='') for role in ROLES})
    if 'slaves' in document:
        result['master'] = document.get('master', {}).get('sudo_password', '')
        for role, slave in document['slaves'].items():
            common = slave.get('password', '')
            result['slaves'][role] = dict(ssh=slave.get('ssh_password', common),
                                         sudo=slave.get('sudo_password', common))
    return result


def remote_path(path):
    return '"$HOME"/' + shlex.quote(path[2:]) if path.startswith('~/') else shlex.quote(path)


def request(config, role, run_id, job_id, argv, timeout=300, **options):
    host = config['hosts'][role]
    peer = config['hosts'][ROLES[1] if role == ROLES[0] else ROLES[0]]
    return dict(repo=host['repo'], ros_setup=host['ros_setup'], rp_bin=host.get('rp_bin', ''),
                run_id=label(run_id), job_id=label(job_id), argv=argv, timeout=timeout,
                env={'ROS_DOMAIN_ID': str(config['domain_id']),
                     'DISCOVERY_ROS_DOMAIN_ID': str(config['domain_id']),
                     'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
                     'NIC': host['nic'], 'ARTIFACT_PEER': peer['address'],
                     'RP_EXP_DATA_ADDRESS': host['address'],
                     'RP_EXP_CLOCK_SOURCE': config.get('master_clock_address', ''),
                     'RP_EXP_REQUIRE_NETWORK_GUARD': '1' if config.get('_network_guard') else '0',
                     'RP_EXP_EXECUTION_PROFILE': 'master-scheduled-v1',
                     'RP_EXP_UNATTENDED': '1'}, **options)


def stages(config, args, target_time=0):
    """No SSH or filesystem effects. Timing inside the existing workers is retained."""
    selected = list(EXPERIMENTS) if args.experiment == 'all' else [args.experiment]
    a, b = (config['hosts'][role] for role in ROLES)
    result = []
    for exp in selected:
        prefix = f'experiments/{EXPERIMENTS[exp]}'
        if exp == '1':
            for np in args.np:
                for condition in ('baseline', 'rp_run', 'ros2_daemon'):
                    for rep in range(1, args.runs + 1):
                        name = f'discovery-G{np}-{condition}-{rep:02d}'
                        jobs = {}
                        for role in ROLES:
                            host = config['hosts'][role]
                            # Workspace expands in slave_controller. Capture is relative to its repo.
                            jobs[role] = request(config, role, args.run_id, name,
                                ['python3', f'{prefix}/scripts/slave_controller.py',
                                 '--target-time', str(target_time), '--N_E', '2', '--N_P', str(np),
                                 '--total-processes', str(np * 2), '--run-duration', '7',
                                 '--capture-output', f'results/{args.run_id}/01_discovery/{role}/G{np}/{condition}/run_{rep:02d}/discovery_capture.pcap',
                                 '--capture-interface', host['nic'], '--observer', condition,
                                 '--observer-lead-sec', '10', '--ros-setup', host['ros_setup'],
                                 '--workspace-setup', host['repo'].rstrip('/') + '/install/setup.bash',
                                 '--rp-command', 'sudo -n "$RP_BIN" run'], timeout=120)
                        result.append(dict(name=name, jobs=jobs, paired=False, cooldown=25))
        else:
            variants = ('rate', 'bag') if exp == '2' else qos_profiles(args) if exp == '4' else ('main',)
            for variant in variants:
                suffix = f'_{variant}' if exp == '2' else ''
                common_a = ['--sync', b['address'], '--platform', 'pc']
                common_b = ['--sync', a['address'], '--platform', 'pc', '--runs', str(args.runs)]
                if exp == '2':
                    port = 55001 if variant == 'rate' else 55101
                    common_b += ['--scenarios', ','.join(args.resource_scenarios)]
                    count = len(args.resource_scenarios) * (3 if variant == 'rate' else 2)
                elif exp == '3':
                    port = 56001
                    common_a += ['--nic', a['nic']]
                    common_b += ['--losses', ','.join(map(str, args.losses))]
                    count = len(args.losses) * 2
                else:
                    port = 55001
                    profile = ['--qos', variant, '--link', config['link']]
                    common_a += profile
                    common_b += profile + ['--nic', b['nic'], '--scenarios', ','.join(args.probe_scenarios)]
                    count = len(args.probe_scenarios) * 5
                name = f'exp{exp}-{variant}'
                timeout = 180 + count * args.runs * 150
                result.append(dict(name=name, paired=True, cooldown=0, jobs={
                    'slave_a': request(config, 'slave_a', args.run_id, name + '-publisher',
                        ['bash', f'{prefix}/scripts/run{suffix}_publishers.sh', *common_a],
                        timeout=timeout, ready_port=port, unused_ports=[port]),
                    'slave_b': request(config, 'slave_b', args.run_id, name + '-receiver',
                        ['bash', f'{prefix}/scripts/run{suffix}_subscribers.sh', *common_b],
                        timeout=timeout, unused_ports=[port + 1], master_schedule=True,
                        schedule_lead_sec=5, schedule_clock_checks=True)}))
                result[-1]['jobs']['slave_b']['env']['RP_EXP_SCHEDULE_TRIGGER'] = 'GO' if exp == '3' else 'START'
                result[-1]['jobs']['slave_b']['env']['RP_EXP_MASTER_CLOCK_BARRIER'] = '1'
    return result


def export_jobs(config, args):
    if args.experiment not in ('all', '3'):
        return []
    return [('slave_b', request(config, 'slave_b', args.run_id, 'export-fidelity-sequences',
        ['python3', 'experiments/03_observation_fidelity/analysis/export_sequences.py',
         '--results-dir', f'results/{args.run_id}/03_observation_fidelity',
         '--bags-dir', f'bags/{args.run_id}/03_observation_fidelity'], timeout=1800))]


def analysis_jobs(config, args, destination=None):
    """Local master commands; no ROS bindings and no slave subprocesses."""
    result = []
    destination = Path(destination or ROOT / 'results' / args.run_id)
    selected = list(EXPERIMENTS) if args.experiment == 'all' else [args.experiment]
    for exp in selected:
        script = f'experiments/{EXPERIMENTS[exp]}/analysis/analyze.py'
        profiles = [(role, None) for role in ROLES] if exp == '1' else (
            [('slave_b', qos) for qos in qos_profiles(args)] if exp == '4' else [('slave_b', None)])
        for role, qos in profiles:
            path = str(destination / role / EXPERIMENTS[exp])
            output = destination / 'analysis' / EXPERIMENTS[exp]
            if exp == '1': output /= role
            if exp == '4': output /= f'{config["link"]}_{qos}'
            if exp == '1':
                peer = ROLES[1] if role == ROLES[0] else ROLES[0]
                argv = ['--base-dir', path, '--peer-base-dir', str(destination / peer / EXPERIMENTS[exp]),
                        '--output-dir', str(output)]
            elif exp == '2':
                argv = ['--results-dir', path + '/rate', '--bag-results-dir', path + '/recording',
                        '--out-dir', str(output)]
            elif exp == '3':
                argv = ['--results-dir', path, '--sequence-csv', '--out-dir', str(output)]
            else:
                argv = ['--results-dir', path + f'/{config["link"]}_{qos}', '--out-dir', str(output)]
            result.append(dict(job_id=f'analysis-{exp}-{role}' + (f'-{qos}' if qos else ''),
                               experiment=exp, role=role, qos=qos, runs_csv=str(output / 'runs.csv'),
                               argv=[script, *argv]))
    return result
