"""Resumable checksum-verified transfers; curl's native meter shows speed and ETA."""
import hashlib
from pathlib import Path
import subprocess

DEFAULTS = {'download_timeout_seconds': 14400, 'download_attempts': 5, 'download_stall_seconds': 90}


def digest(path, algorithm='sha256'):
    try:
        with Path(path).open('rb') as stream:
            return hashlib.file_digest(stream, algorithm).hexdigest()
    except OSError as error:
        raise OSError(error.errno, f'Cannot hash file: {error.strerror}', str(path)) from error


def download(url, destination, expected=None, algorithm='sha256', size=None, settings=None, progress=True):
    settings = settings or {}
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)

    def valid(candidate):
        return (candidate.is_file() and (size is None or candidate.stat().st_size == size)
                and (expected is None or digest(candidate, algorithm) == expected))

    if valid(path):
        print('Verified cached file:', path.name, flush=True)
        return
    partial = path.with_suffix(path.suffix + '.part')
    if expected is not None and valid(partial):
        partial.replace(path)
        print('Verified completed partial:', path.name, flush=True)
        return
    attempts = settings.get('download_attempts', DEFAULTS['download_attempts'])
    failure = 'not started'
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        print(f'Downloading {path.name}: attempt {attempt + 1}/{attempts}, resume at {offset} bytes', flush=True)
        cmd = ['curl', '--fail', '--location', '--show-error']
        if not progress:
            cmd.append('--silent')
        cmd += ['--connect-timeout', '20', '--max-time',
                str(settings.get('download_timeout_seconds', DEFAULTS['download_timeout_seconds'])),
                '--speed-limit', '1', '--speed-time',
                str(settings.get('download_stall_seconds', DEFAULTS['download_stall_seconds'])),
                '--continue-at', '-', '--output', str(partial), url]
        result = subprocess.run(cmd)
        if valid(partial) and (expected is not None or result.returncode == 0):
            partial.replace(path)
            print('Verified:', path.name, flush=True)
            return
        if result.returncode == 0:
            failure = 'completed transfer has an incorrect ' + algorithm + ' or size'
            partial.unlink(missing_ok=True)
        elif result.returncode == 33:
            failure = 'server refused resume (curl exit 33); restarting transfer'
            partial.unlink(missing_ok=True)
        else:
            retained = partial.stat().st_size if partial.exists() else 0
            failure = f'incomplete transfer (curl exit {result.returncode}); {retained} bytes retained'
        print(failure, flush=True)
    raise ValueError(f'Download failed: {path.name}: {failure}. Rerun fetch to resume retained data.')
