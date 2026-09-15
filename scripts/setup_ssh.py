#!/usr/bin/env python3
"""Prepare SSH key access from the master using the private testbed JSON."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

from ssh_access import prepare_access, prepare_local_client
from testbed import ROOT, credentials, default_config, label, load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=default_config())
    parser.add_argument('--link', choices=['wired', 'wireless'], help='required SSH link (default: config default_link)')
    parser.add_argument('--register-ssh-key', action='store_true', help='register the project key even if another key works')
    parser.add_argument('--run-id', type=label, default='ssh-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    parser.add_argument('--dry-run', action='store_true', help='show scope without SSH, installation or writes')
    args = parser.parse_args()
    config = load(args.config, args.link)
    if args.dry_run:
        print(json.dumps(dict(config=config, actions=['prepare master SSH client if missing',
              'verify or register SSH keys for both slaves', 'verify two distinct slave identities',
              'close SSH connections; retain registered keys']), indent=2))
        return 0
    secrets = credentials(args.config)
    (ROOT / 'results').mkdir(exist_ok=True)
    with (ROOT / 'results/.orchestrator.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another master operation is active; finish it before SSH setup')
        output = ROOT / 'results' / args.run_id / '_master'
        output.mkdir(parents=True, exist_ok=False)
        report = dict(config=config, status='incomplete', hosts={})
        try:
            prepare_local_client(secrets['master'])
            report['hosts'] = prepare_access(config, args.config, force_password=args.register_ssh_key)
            report['status'] = 'complete'
        except BaseException as exc:
            report.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
            raise
        finally:
            secrets = None
            (output / 'ssh.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'[master] SSH setup complete: {output / "ssh.json"}', flush=True)
    print('Next: ./install_dependencies.sh', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
