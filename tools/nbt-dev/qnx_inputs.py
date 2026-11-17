"""Select versioned C/C++ inputs and prepare an isolated QNX build context."""
from pathlib import Path
import shutil
import stat
import subprocess

INPUT_ROOTS = ('Dockerfile', 'sdp', 'gcc', 'binutils', 'tools', 'cross', 'entrypoint.sh')


def compiler_inputs(tree):
    tree = Path(tree)
    result = subprocess.run(['git', '-C', str(tree), 'ls-files', '-z', '--', *INPUT_ROOTS],
                            check=True, capture_output=True)
    paths = []
    for name in result.stdout.decode().split('\0'):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid QNX input path: ' + name)
        path = tree / relative
        if not path.is_symlink():
            info = path.stat()
            if getattr(info, 'st_flags', 0) & getattr(stat, 'SF_DATALESS', 0x40000000):
                raise ValueError('Required QNX input is not stored locally; restore it before retrying: ' + str(path))
        paths.append(path)
    if not paths:
        raise ValueError('No versioned QNX compiler inputs found: ' + str(tree))
    return paths


def prepare_context(tree, destination):
    tree, destination = Path(tree), Path(destination)
    paths = compiler_inputs(tree)
    # This directory contains generated copies only, never original source files.
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for source in paths:
        target = destination / source.relative_to(tree)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(source.readlink())
        else:
            shutil.copy2(source, target)
    return destination
