"""Select versioned C/C++ inputs and prepare an isolated QNX build context."""
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

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
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=destination.name + '.staging-', dir=destination.parent))
    previous = staging.with_name(staging.name.replace('.staging-', '.previous-', 1))
    moved_previous = False
    try:
        for source in paths:
            target = staging / source.relative_to(tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(source.readlink())
            else:
                shutil.copy2(source, target)
        # Recursive removal can race with files created by desktop services.
        # Preserve the old tree; publishing must not depend on deleting it.
        if destination.exists() or destination.is_symlink():
            destination.rename(previous)
            moved_previous = True
        try:
            staging.rename(destination)
        except OSError:
            if moved_previous:
                previous.rename(destination)
            raise
    except Exception:
        print('Unpublished QNX context retained: ' + str(staging), flush=True)
        raise
    if moved_previous:
        print('Previous QNX context retained: ' + str(previous), flush=True)
    return destination
