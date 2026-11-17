#!/usr/bin/env python3
"""Check tracked files before publishing. Does not inspect ignored private data."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_VIN = b'WBA00000000000000'

def issues(path, raw):
    result = []
    if path.startswith(('research/', 'private/', '.build/', 'tools/qnx/', 'tools/qnx-deps/')) or path.endswith(('.local.json', '.local.yml')):
        result.append('private path')
    if b'\x00' in raw:
        result.append('binary input requires separate review')
    # VINs can occur as readable text or inside a contiguous hexadecimal response.
    samples = [raw]
    for value in re.findall(rb'(?<![0-9a-fA-F])[0-9a-fA-F]{24,}(?![0-9a-fA-F])', raw):
        if len(value) % 2 == 0:
            samples.append(bytes.fromhex(value.decode()))
    for sample in samples:
        if any(v != ALLOWED_VIN for v in re.findall(rb'WBA[A-HJ-NPR-Z0-9]{14}', sample)):
            result.append('vehicle identifier')
    if re.search(rb'/Users/[A-Za-z0-9_-]+/', raw):
        result.append('personal home path')
    if re.search(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----', raw):
        result.append('private key')
    if re.search(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})', raw):
        result.append('GitHub credential')
    return sorted(set(result))

def main():
    failures = []
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    for name in filter(None, names):
        for problem in issues(name, (ROOT / name).read_bytes()):
            failures.append(f'{name}: {problem}')
    print('\n'.join(failures) if failures else 'Public tracked-file checks passed.')
    return int(bool(failures))

if __name__ == '__main__':
    sys.exit(main())
