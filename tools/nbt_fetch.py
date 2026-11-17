#!/usr/bin/env python3
"""Visible dependency acquisition; check verifies the same resources without downloading."""
import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from nbt_download import digest, download
import nbt_re as RE

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.build/downloads'
RECEIPT = CACHE / 'ready.json'


def load_helper(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command):
    print('+ ' + ' '.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=ROOT, check=True)


def read_json(path):
    return json.loads(Path(path).read_text())


def image_info(reference, architecture):
    platform = 'linux/' + architecture
    result = subprocess.run(['docker', 'image', 'inspect', '--platform', platform, reference], capture_output=True, text=True)
    if result.returncode:
        error = result.stderr.strip()
        if any(marker in error.lower() for marker in ('no such image', 'not found', 'does not provide', 'does not match')):
            return None
        raise ValueError(f'Cannot inspect {platform} for {reference}: {error}')
    info = json.loads(result.stdout)[0]
    if info.get('Os') != 'linux' or info.get('Architecture') != architecture:
        observed = f"{info.get('Os') or 'unknown'}/{info.get('Architecture') or 'unknown'}"
        raise ValueError(f'Unexpected image platform: expected {platform}, got {observed}: {reference}')
    return info


def bases(cfg):
    dev = (ROOT / 'tools/nbt-dev/Dockerfile').read_text().splitlines()[0].split()[1]
    re_base = cfg.get('base_image') or (ROOT / 'tools/nbt-re/Dockerfile').read_text().splitlines()[0].split('=', 1)[1]
    return {'dev': (dev, 'amd64'),
            'qnx': (read_json(ROOT / 'tools/nbt-dev/dependencies-arm64.json')['base_image'], 'arm64'),
            're': (re_base, 'arm64')}


def fingerprints():
    paths = ['tools/nbt_fetch.py', 'tools/nbt_download.py', 'tools/nbt-prefetch.sh',
             'tools/nbt-dev/Dockerfile', 'tools/nbt-dev/apt-packages.txt',
             'tools/nbt-dev/python-requirements.txt', 'tools/nbt-dev/toolchain.lock.json',
             'tools/nbt-dev/dependencies-arm64.json', 'tools/nbt-dev/prepare-qnx-build.py',
             'tools/nbt-dev/prepare-qnx-metadata.py', 'tools/nbt-re/Dockerfile',
             'tools/nbt-re/apt-packages.txt', 'tools/nbt-re/artifacts.lock.json']
    return {p: digest(ROOT / p) for p in paths}


def snapshot(paths):
    files = {}
    for path in paths:
        candidates = [path] if path.is_file() else path.rglob('*')
        for p in candidates:
            if p.is_file() and not p.is_symlink() and '.git' not in p.relative_to(ROOT).parts:
                files[str(p.relative_to(ROOT))] = digest(p)
    return files


def validate_files(files):
    if not files:
        raise ValueError('Empty dependency inventory; run tools/nbt-env fetch')
    for relative, expected in files.items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file() or digest(path) != expected:
            raise ValueError('Missing/changed dependency: ' + relative + '; run tools/nbt-env fetch')


def check(components, cfg):
    if not RECEIPT.is_file():
        raise ValueError('Dependencies not prepared. First run: ./tools/nbt-env fetch')
    receipt = read_json(RECEIPT)
    if receipt.get('fingerprints') != fingerprints():
        raise ValueError('Dependency recipes changed. Rerun: ./tools/nbt-env fetch')
    image_specs = bases(cfg)
    for component in components:
        print('Verifying downloaded resources:', component, flush=True)
        entry = receipt.get('components', {}).get(component)
        if not entry:
            raise ValueError('Missing dependency component: ' + component)
        validate_files(entry['files'])
        reference, architecture = image_specs[component]
        if entry['base'] != reference or not image_info(reference, architecture):
            raise ValueError('Missing/changed base image; run tools/nbt-env fetch: ' + reference)
    print('Downloaded resources verified. No downloads performed.', flush=True)


def fetch_qnx_tree():
    lock = read_json(ROOT / 'tools/nbt-dev/toolchain.lock.json')
    destination = ROOT / 'tools/qnx'
    if not (destination / 'Dockerfile').is_file():
        if destination.exists():
            raise ValueError('Incomplete tools/qnx exists; inspect/move it before retrying')
        staging = CACHE / 'qnx-checkout'
        if not staging.exists():
            run(['git', 'init', staging])
        elif not (staging / '.git').is_dir():
            raise ValueError('Unexpected .build/downloads/qnx-checkout; inspect/move it before retrying')
        run(['git', '-C', staging, 'fetch', '--progress', '--depth=1', lock['repository'], lock['commit']])
        run(['git', '-C', staging, 'checkout', '--detach', lock['commit']])
        # A checkout is installed only after verifying its critical inputs.
        verify_qnx_tree(staging, lock)
        staging.rename(destination)
    verify_qnx_tree(destination, lock)
    return destination


def verify_qnx_tree(directory, lock):
    for item in lock['files']:
        p = directory / item['path']
        if not p.is_file() or digest(p) != item['sha256']:
            raise ValueError('Pinned QNX input mismatch: ' + item['path'])


def qnx_compiler_inputs(tree):
    # base-env builds the C/C++ toolchain. Go/Rust test fixtures are not build inputs.
    return [tree / name for name in ('Dockerfile', 'sdp', 'gcc', 'binutils', 'tools', 'cross', 'entrypoint.sh')]


def fetch_qnx(settings):
    tree = fetch_qnx_tree()
    deps = load_helper('qnx_deps', 'tools/nbt-dev/qnx-deps.py')
    packages = read_json(deps.MANIFEST)['packages']
    # Normalize prior manual/browser downloads before requesting missing packages.
    for index, item in enumerate(deps.normalize(packages), 1):
        print(f'Debian package {index}: {item["filename"]}', flush=True)
        download(item['url'], deps.DOWNLOADS / item['filename'], item['sha256'], size=item['size'], settings=settings)
    missing = deps.normalize(packages)
    if missing:
        raise ValueError('Debian package preparation incomplete')
    gnu = load_helper('qnx_sources', 'tools/nbt-dev/prepare-qnx-build.py')
    for remote, name, checksum in gnu.SOURCES:
        last = None
        for mirror in gnu.MIRRORS:
            try:
                download(mirror + remote, gnu.CACHE / name, checksum, settings=settings)
                break
            except ValueError as error:
                last = error
        else:
            raise last
    metadata = load_helper('qnx_metadata', 'tools/nbt-dev/prepare-qnx-metadata.py')
    for local, archive, suite in metadata.SUITES:
        remote = f'https://snapshot.debian.org/archive/{archive}/{metadata.SNAPSHOT}/dists/{suite}/'
        target = metadata.CACHE / local / 'dists' / suite
        release = target / 'InRelease'
        download(remote + 'InRelease', release, settings=settings)
        checksums = {}
        inside = False
        for line in release.read_text().splitlines():
            if line == 'SHA256:':
                inside = True
            elif inside and line.startswith(' '):
                sha, size, name = line.split()
                checksums[name] = (sha, int(size))
            elif inside:
                break
        for arch in ('arm64', 'i386'):
            name = f'main/binary-{arch}/Packages.xz'
            sha, size = checksums[name]
            download(remote + name, target / name, sha, size=size, settings=settings)
        metadata.prepare_suite((local, archive, suite), offline=True)
    run([sys.executable, 'tools/nbt-dev/prepare-qnx-build.py', '--offline', '--platform', 'linux/arm64'])
    return qnx_compiler_inputs(tree) + [deps.CACHE, gnu.CACHE, metadata.CACHE]


def docker_dependency_step(component, reference, architecture, mode, artifacts=None):
    directory = CACHE / component
    for folder in ('apt/lists/partial', 'apt/archives/partial', 'wheels' if component == 'dev' else 'npm'):
        (directory / folder).mkdir(parents=True, exist_ok=True)
    name = 'nbt-fetch-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '--name', name, '--pull', 'never', '--platform', 'linux/' + architecture,
               '--network', 'bridge' if mode in ('apt', 'wheels', 'npm') else 'none']
    if sys.stdout.isatty():
        command.append('-t')
    mounts = [(directory, '/cache', False), (ROOT / ('tools/nbt-dev' if component == 'dev' else 'tools/nbt-re'), '/recipes', True),
              (ROOT / 'tools/nbt-prefetch.sh', '/prefetch.sh', True),
              (directory / 'apt/lists', '/var/lib/apt/lists', False),
              (directory / 'apt/archives', '/var/cache/apt/archives', False)]
    if artifacts:
        mounts.append((artifacts, '/artifacts', True))
    for source, destination, readonly in mounts:
        command += ['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if readonly else '')]
    command += ['--env', 'NBT_APT_I386=' + ('1' if component == 'dev' else '0'),
                '--entrypoint', '/bin/sh', reference, '/prefetch.sh', mode]
    try:
        run(command)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fetch(components, cfg):
    image_specs = bases(cfg)
    result = {'utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'components': {}}
    # An unsuccessful/aborted refresh must never leave an old readiness certificate.
    RECEIPT.unlink(missing_ok=True)
    for index, component in enumerate(components, 1):
        print(f'\n=== [{index}/{len(components)}] {component} ===', flush=True)
        reference, architecture = image_specs[component]
        if not image_info(reference, architecture):
            run(['docker', 'pull', '--platform', 'linux/' + architecture, reference])
        if not image_info(reference, architecture):
            raise ValueError('Base image unavailable after pull: ' + reference)
        if component == 'qnx':
            paths = fetch_qnx(cfg)
        else:
            paths = []
            if component == 're':
                for item in read_json(ROOT / 'tools/nbt-re/artifacts.lock.json').values():
                    algorithm = 'sha512' if 'sha512' in item else 'sha256'
                    download(item['url'], cfg['data'] / 'artifacts' / item['file'], item[algorithm],
                             algorithm=algorithm, settings=cfg)
                paths.append(cfg['data'] / 'artifacts')
            docker_dependency_step(component, reference, architecture, 'apt')
            docker_dependency_step(component, reference, architecture,
                                   'wheels' if component == 'dev' else 'npm',
                                   cfg['data'] / 'artifacts' if component == 're' else None)
            paths.append(CACHE / component)
        result['components'][component] = {'base': reference, 'files': snapshot(paths)}
    result['fingerprints'] = fingerprints()
    temporary = RECEIPT.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.replace(RECEIPT)
    print('\nAll selected dependencies are ready. Next: ./tools/nbt-env setup', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('fetch', 'check'))
    parser.add_argument('--components', nargs='+', choices=('dev', 'qnx', 're'), default=['dev', 'qnx', 're'])
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11+ required')
    if ',' in str(ROOT):
        raise ValueError('Docker bind mounts cannot use a repository path containing commas')
    cfg = RE.config(RE.LOCAL if RE.LOCAL.exists() else RE.EXAMPLE)
    if args.command == 'check':
        check(args.components, cfg)
        return
    for tool in ('docker', 'git', 'curl'):
        if not shutil.which(tool):
            raise ValueError('Missing tool: ' + tool)
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / '.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another fetch is running; wait for it to finish') from None
        fetch(list(dict.fromkeys(args.components)), cfg)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print('Fetch error:', error, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print('\nInterrupted; completed resources and partial transfers are retained.', file=sys.stderr)
        sys.exit(130)
