#!/usr/bin/env python3
"""Install the bundled ros2probe with Cargo into this checkout's .tools/bin/rp."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BPF_LINKER_VERSION = '0.10.2'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_identity(root=ROOT):
    source = root / 'vendor/ros2probe'
    files = [p for p in source.rglob('*') if p.is_file() and 'target' not in p.relative_to(source).parts]
    if not (source / 'Cargo.lock').is_file() or not (source / 'ros2probe/Cargo.toml').is_file():
        raise RuntimeError('Bundled ros2probe sources are missing; deploy this checkout first')
    hashes = {str(p.relative_to(source)):sha256(p) for p in sorted(files)}
    text = (source / 'ros2probe/src/capture/socket.rs').read_text()
    if not re.search(r'CAPTURE_RING_BLOCK_SIZE:\s*usize\s*=\s*256\s*\*\s*1024;', text) or not re.search(
            r'CAPTURE_RING_BLOCK_COUNT:\s*usize\s*=\s*8;', text):
        raise RuntimeError('This artifact requires the bundled 256 KiB x 8 capture ring')
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return dict(source_sha256=digest, files_sha256=hashes, capture_ring_bytes_per_interface=2097152)


def installed_manifest(root=ROOT):
    binary = root / '.tools/bin/rp'
    path = root / '.tools/rp-build.json'
    if not binary.is_file() or not path.is_file():
        raise RuntimeError('Build bundled rp first: python3 scripts/install_rp.py --setup-toolchain')
    manifest = json.loads(path.read_text())
    identity = source_identity(root)
    if (manifest.get('status') != 'complete' or manifest.get('source_sha256') != identity['source_sha256']
            or manifest.get('binary_sha256') != sha256(binary)
            or manifest.get('capture_ring_bytes_per_interface') != 2097152):
        raise RuntimeError('Installed rp does not match bundled sources; rerun python3 scripts/install_rp.py')
    return manifest


def run(argv, env):
    print('[rp-build] ' + ' '.join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), env=env, check=True, stdin=subprocess.DEVNULL)


def output(argv, env):
    return subprocess.check_output(argv, env=env, text=True, stdin=subprocess.DEVNULL).strip()


def prepare_tools(env, automatic):
    def available(command):
        return shutil.which(command, path=env['PATH'])

    if not available('rustup'):
        if not automatic:
            raise RuntimeError('Missing rustup; run install_rp.py --setup-toolchain')
        with tempfile.TemporaryDirectory(prefix='ros2probe-rustup-') as folder:
            installer = Path(folder) / 'rustup-init.sh'
            run(['curl', '--proto', '=https', '--tlsv1.2', '--fail', '--location',
                 'https://sh.rustup.rs', '--output', installer], env)
            run(['sh', installer, '-y', '--profile', 'minimal', '--default-toolchain', 'stable',
                 '--no-modify-path'], env)
    for toolchain in ('stable', 'nightly'):
        result = subprocess.run(['rustup', 'which', '--toolchain', toolchain, 'rustc'],
                                env=env, capture_output=True)
        if result.returncode:
            if not automatic:
                raise RuntimeError(f'Missing {toolchain}; run install_rp.py --setup-toolchain')
            run(['rustup', 'toolchain', 'install', toolchain, '--profile', 'minimal'], env)
    components = output(['rustup', 'component', 'list', '--toolchain', 'nightly', '--installed'], env)
    if 'rust-src' not in components.splitlines():
        if not automatic:
            raise RuntimeError('Missing nightly rust-src; run install_rp.py --setup-toolchain')
        run(['rustup', 'component', 'add', 'rust-src', '--toolchain', 'nightly'], env)
    linker = output(['bpf-linker', '--version'], env) if available('bpf-linker') else ''
    if linker != 'bpf-linker ' + BPF_LINKER_VERSION:
        if not automatic:
            raise RuntimeError(f'Need bpf-linker {BPF_LINKER_VERSION}; run install_rp.py --setup-toolchain')
        run(['cargo', '+stable', 'install', '--locked', 'bpf-linker', '--version', BPF_LINKER_VERSION,
             '--root', ROOT / '.tools/build-tools', '--jobs', '2'], env)
    return dict(rustc=output(['rustc', '+stable', '--version', '--verbose'], env),
                cargo=output(['cargo', '+stable', '--version'], env),
                ebpf_rustc=output(['rustc', '+nightly', '--version', '--verbose'], env),
                bpf_linker=output(['bpf-linker', '--version'], env))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup-toolchain', action='store_true',
                        help='install missing Rust toolchains/rust-src and bpf-linker as this user')
    parser.add_argument('--force', action='store_true', help='invoke cargo install even if the verified build is current')
    parser.add_argument('--verify', action='store_true', help='check source/binary hashes and print the saved manifest')
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(installed_manifest(), indent=2))
        return
    if os.geteuid() == 0:
        parser.error('Build rp as the experiment user, without sudo; only rp run needs root')
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    if os.environ.get('AYA_BUILD_SKIP') not in (None, '', '0'):
        parser.error('AYA_BUILD_SKIP would omit eBPF; unset it for the artifact build')
    identity = source_identity()
    tools_dir = ROOT / '.tools'
    tools_dir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env['PATH'] = os.pathsep.join([str(tools_dir / 'build-tools/bin'), str(Path.home() / '.cargo/bin'),
                                 env.get('PATH', '')])
    with ExitStack() as stack:
        # remote_job holds this lock throughout supervised setup/build jobs.
        if os.environ.get('RP_EXP_SUPERVISED') != '1':
            (ROOT / 'results').mkdir(exist_ok=True)
            lock = stack.enter_context((ROOT / 'results/.master.lock').open('a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = stack.enter_context((tools_dir / 'rp-build.lock').open('a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.force:
            try:
                installed_manifest()
            except (OSError, ValueError, RuntimeError):
                pass
            else:
                print('[rp-build] Bundled source and installed binary match; reuse .tools/bin/rp')
                return
        versions = prepare_tools(env, args.setup_toolchain)
        command = ['cargo', '+stable', 'install', '--locked', '--path', str(ROOT / 'vendor/ros2probe/ros2probe'),
                   '--root', str(tools_dir), '--target-dir', str(tools_dir / 'rp-target'),
                   '--bin', 'rp', '--jobs', str(args.jobs), '--force']
        # Keep upstream default features, including GUI, so only ring size changes.
        run(command, env)
        if source_identity()['source_sha256'] != identity['source_sha256']:
            raise RuntimeError('Source or Cargo.lock changed during build; inspect before accepting the binary')
        binary = tools_dir / 'bin/rp'
        subprocess.run([str(binary), '--help'], env=env, check=True, capture_output=True)
        provenance = json.loads((ROOT / 'vendor/ros2probe-source.json').read_text())
        manifest = dict(identity, status='complete', binary_sha256=sha256(binary),
                        built_at=datetime.now(timezone.utc).isoformat(), binary=str(binary),
                        upstream_commit=provenance['upstream_commit'], toolchain=versions,
                        cargo_command=command, cargo_features='upstream defaults (gui)',
                        build_environment={k:env[k] for k in ('RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS',
                                           'CARGO_BUILD_TARGET') if k in env})
        temporary = tools_dir / 'rp-build.json.tmp'
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        temporary.replace(tools_dir / 'rp-build.json')
        print('[rp-build] Complete: ' + str(binary), flush=True)
        print('[rp-build] SHA-256: ' + manifest['binary_sha256'], flush=True)


if __name__ == '__main__':
    main()
