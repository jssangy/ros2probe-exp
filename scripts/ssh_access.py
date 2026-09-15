"""First-use SSH key installation; passwords are read by SSH_ASKPASS from private JSON."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from testbed import ROOT, ROLES, credentials

SESSION_OPTIONS = ['-o', 'ControlMaster=no', '-o', 'ControlPath=none', '-o', 'ControlPersist=no']
SSH_OPTIONS = ['-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3', *SESSION_OPTIONS]
KEY = ROOT / '.tools/ssh/id_ed25519'


def ssh_options():
    return SSH_OPTIONS + (['-i', str(KEY)] if KEY.is_file() else [])


def check_access(host):
    """Verify prior SSH setup without registering keys or accepting a new host key."""
    result = subprocess.run(['ssh', *ssh_options(), '-o', 'StrictHostKeyChecking=yes',
                             host['ssh'], 'true'], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('SSH is not ready for ' + host['ssh'] + '; run ./install_dependencies.sh or configure SSH credentials')


def prepare_local_client(password):
    if shutil.which('ssh') and shutil.which('ssh-keygen'):
        return
    code = ('import os,sys; sys.path.insert(0,' + repr(str(ROOT / 'scripts')) + '); '
            'from bootstrap_host import install_packages; '
            'install_packages(["openssh-client"],dict(os.environ,DEBIAN_FRONTEND="noninteractive"))')
    argv = [sys.executable, '-c', code]
    if password:
        subprocess.run(['sudo', '-S', '-p', '', '--', *argv], check=True, input=password + '\n', text=True)
    else:
        subprocess.run(['sudo', '--', *argv], check=True)


def prepare_access(config, config_path, *, require_ready=False, force_password=False):
    """Verify/register only the selected link, closing each SSH command on return."""
    secrets = credentials(config_path)
    details = {}
    try:
        for role in ROLES:
            host = config['hosts'][role]
            if require_ready:
                check_access(host)
                method = 'verified-existing-key'
            else:
                method = ensure_access(host, config_path, role, secrets['slaves'][role]['ssh'],
                                       force_password=force_password)
            details[role] = dict(host_info(host), authentication=method)
            print(f'[master] {role}: SSH ready at {host["ssh"]} ({method})', flush=True)
        verify_distinct_hosts(details)
        return details
    finally:
        secrets = None


def host_info(host):
    code = ('import json,os,pathlib,platform,pwd; '
            'print(json.dumps(dict(user=pwd.getpwuid(os.getuid()).pw_name, '
            'home=str(pathlib.Path.home()), arch=platform.machine(), os_release=platform.freedesktop_os_release(), '
            'machine_id=pathlib.Path("/etc/machine-id").read_text().strip())))')
    text = subprocess.check_output(['ssh', *ssh_options(), host['ssh'],
                                    shlex.join(['python3', '-c', code])], text=True, timeout=30)
    value = json.loads(text)
    value['repo'] = (str(Path(value['home']) / host['repo'][2:])
                     if host['repo'].startswith('~/') else host['repo'])
    return value


def verify_distinct_hosts(details):
    identities = [value['machine_id'] for value in details.values()]
    if len(set(identities)) != 2 or Path('/etc/machine-id').read_text().strip() in identities:
        raise RuntimeError('Setup requires two distinct slaves separate from the master')


def ensure_key():
    KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    KEY.parent.chmod(0o700)
    if not KEY.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'ros2probe-exp',
                        '-f', str(KEY)], check=True, stdin=subprocess.DEVNULL)
    return KEY.with_suffix('.pub').read_text().strip()


def password_options():
    return ['-T', '-o', 'BatchMode=no', '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', 'NumberOfPasswordPrompts=1',
            '-o', 'PreferredAuthentications=password,keyboard-interactive',
            '-o', 'PubkeyAuthentication=no', *SESSION_OPTIONS]


def password_environment(config_path, role):
    return dict(os.environ, SSH_ASKPASS=str(ROOT / 'scripts/ssh_askpass.py'),
                SSH_ASKPASS_REQUIRE='force', DISPLAY='ros2probe-setup',
                RP_EXP_CREDENTIALS_FILE=str(Path(config_path).resolve()),
                RP_EXP_CREDENTIALS_ROLE=role)


def ensure_access(host, config_path, role, password, force_password=False):
    probe = ['ssh', *ssh_options(), '-o', 'StrictHostKeyChecking=accept-new', host['ssh'], 'true']
    result = subprocess.run(probe, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    if result.returncode == 0 and not force_password:
        return 'existing-key'
    if not password:
        raise RuntimeError(role + ': SSH key login failed; configure password in the private JSON or register a key')
    key = ensure_key()
    code = ('from pathlib import Path; '
            'p=Path.home()/".ssh"; p.mkdir(mode=0o700,exist_ok=True); p.chmod(0o700); '
            'f=p/"authorized_keys"; t=f.read_text() if f.exists() else ""; '
            'k=' + repr(key) + '; '
            'f.write_text(t+("\\n" if t and not t.endswith("\\n") else "")+k+"\\n") if k not in t.splitlines() else None; '
            'f.chmod(0o600)')
    argv = ['ssh', *password_options(), host['ssh'], shlex.join(['python3', '-c', code])]
    result = subprocess.run(argv, env=password_environment(config_path, role),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            start_new_session=True, timeout=45)
    if result.returncode:
        # Do not include any authentication output in persistent logs.
        raise RuntimeError(role + ': SSH password/key registration failed; check account, password and host key')
    result = subprocess.run(['ssh', *ssh_options(), '-o', 'IdentitiesOnly=yes',
                            '-o', 'StrictHostKeyChecking=accept-new', host['ssh'], 'true'],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(role + ': registered key did not pass noninteractive SSH verification')
    return 'project-key-registered'
