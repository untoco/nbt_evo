"""Update from the publisher; open checks local files and explicitly launches the client."""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
from pathlib import Path, PurePosixPath
import platform
import plistlib
import posixpath
import re
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

from nbt_download import digest, download

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name('bavarian.lock.json')
CHECKSUMS = 'https://ssh.bavarian-aa.uk/downloads/SHA256SUMS'


def current_file():
    return ROOT / '.build/bavarian/current.json'


def specification():
    item = json.loads(LOCK.read_text())
    if current_file().is_file():
        selected = json.loads(current_file().read_text())
        checksum = selected['sha256']
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError('Invalid local Bavarian version record; run update')
        # The publisher URL, filename and app name always come from our recipe.
        item.update(sha256=checksum, checked_on=selected['checked_on'],
                    reported_release=selected.get('reported_release'))
    return item


def latest_specification():
    item = json.loads(LOCK.read_text())
    print('Checking Bavarian publisher:', CHECKSUMS, flush=True)
    result = subprocess.run(['curl', '--fail', '--location', '--show-error', '--silent',
                             '--proto', '=https', '--proto-redir', '=https',
                             '--connect-timeout', '20', '--max-time', '45',
                             '--header', 'Cache-Control: no-cache', CHECKSUMS],
                            capture_output=True, text=True, check=True)
    matches = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?([^\s]+)', line.strip())
        if match and match[2] == item['file']:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise ValueError('Publisher checksum index must contain exactly one macOS ZIP entry')
    if matches[0] != item['sha256']:
        item['reported_release'] = None  # The checksum index has no version numbers.
    item.update(sha256=matches[0], checked_on=dt.date.today().isoformat())
    return item


def locations(item):
    directory = ROOT / '.build/bavarian' / item['sha256']
    return directory, directory / item['file'], directory / 'prepared'


@contextmanager
def exclusive():
    directory = ROOT / '.build/bavarian'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Bavarian update/fetch/prepare is running') from None
        yield


def verify_archive(item):
    _, archive, _ = locations(item)
    if not archive.is_file() or digest(archive) != item['sha256']:
        raise ValueError('Missing/changed Bavarian archive. Run: ./tools/nbt-bavarian update')
    return archive


def fetch(settings=None):
    item = specification()
    _, archive, _ = locations(item)
    with exclusive():
        download(item['url'], archive, item['sha256'], settings=settings)
        verify_archive(item)
    print('Bavarian archive verified. Next: ./tools/nbt-bavarian prepare', flush=True)


def require_macos():
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11+ required')
    if platform.system() != 'Darwin':
        raise ValueError('Bavarian update/prepare/check/open require macOS')


def validate_zip(archive):
    """Reject escapes and writes through archive symlinks before invoking ditto."""
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        links = set()
        names = set()
        for member in members:
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.filename
                    or not path.parts or path.parts[0].endswith(':')):
                raise ValueError('Unsafe ZIP path: ' + member.filename)
            name = str(path)
            if name in names:
                raise ValueError('Duplicate ZIP path: ' + name)
            names.add(name)
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                raise ValueError('Unsupported ZIP entry: ' + name)
            if stat.S_ISLNK(mode):
                target = package.read(member).decode('utf-8')
                resolved = posixpath.normpath(posixpath.join(str(path.parent), target))
                if (not target or target.startswith('/') or '\\' in target or '\x00' in target
                        or resolved == '..' or resolved.startswith('../')):
                    raise ValueError('Unsafe ZIP symlink: ' + name)
                links.add(path)
        for member in members:
            if any(parent in links for parent in PurePosixPath(member.filename).parents):
                raise ValueError('ZIP entry writes through symlink: ' + member.filename)


def inventory(app):
    result = {}
    for path in sorted(app.rglob('*')):
        relative = str(path.relative_to(app))
        if path.is_symlink():
            if not path.resolve().is_relative_to(app.resolve()):
                raise ValueError('Bundle symlink escapes app: ' + relative)
            result[relative] = {'link': str(path.readlink())}
        elif path.is_file():
            result[relative] = {'sha256': digest(path), 'mode': stat.S_IMODE(path.stat().st_mode)}
        elif not path.is_dir():
            raise ValueError('Unsupported bundle entry: ' + relative)
    if not result:
        raise ValueError('Empty Bavarian bundle')
    return result


def bundle_info(app):
    with (app / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    executable = info.get('CFBundleExecutable', '')
    if not executable or Path(executable).name != executable or executable in ('.', '..'):
        raise ValueError('Invalid bundle executable')
    binary = app / 'Contents/MacOS' / executable
    if not binary.is_file() or not binary.stat().st_mode & 0o111:
        raise ValueError('Missing/non-executable bundle binary')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    architectures = subprocess.run(['/usr/bin/lipo', '-archs', str(binary)],
                                   capture_output=True, text=True, check=True).stdout.strip().split()
    host = platform.machine()
    if host not in architectures:
        raise ValueError(f'Bavarian binary does not support host {host}: {architectures}')
    return {'bundle_version': info.get('CFBundleShortVersionString'),
            'bundle_build': info.get('CFBundleVersion'), 'architectures': architectures}


def check(item=None):
    require_macos()
    item = item or specification()
    verify_archive(item)
    _, _, prepared = locations(item)
    app = prepared / item['bundle']
    receipt = prepared / 'receipt.json'
    if not receipt.is_file() or not app.is_dir() or app.is_symlink():
        raise ValueError('Bavarian is not prepared. Run: ./tools/nbt-bavarian update')
    saved = json.loads(receipt.read_text())
    if saved.get('archive_sha256') != item['sha256'] or saved.get('files') != inventory(app):
        raise ValueError('Prepared Bavarian bundle changed; inspect it before preparing again')
    observed = bundle_info(app)
    if observed != saved.get('observed'):
        raise ValueError('Bavarian bundle metadata changed')
    print('Verified Bavarian:', app, flush=True)
    print('Publisher changelog:', item.get('reported_release') or 'unknown',
          '| Bundle:', json.dumps(observed), flush=True)
    return app


def prepare_locked(item, repair=False):
    archive = verify_archive(item)
    directory, _, prepared = locations(item)
    if prepared.exists():
        try:
            return check(item)
        except (ValueError, OSError, KeyError, plistlib.InvalidFileException,
                subprocess.CalledProcessError):
            if not repair:
                raise
            previous = directory / ('prepared.previous-' + uuid.uuid4().hex[:12])
            prepared.rename(previous)
            print('Changed/incomplete bundle retained:', previous, flush=True)
    validate_zip(archive)
    # A failed extraction or signature check never publishes a prepared bundle.
    with tempfile.TemporaryDirectory(prefix='prepare-', dir=directory) as temporary:
        stage = Path(temporary)
        subprocess.run(['/usr/bin/ditto', '--norsrc', '--noextattr', '--qtn', '--noacl',
                        '-x', '-k', str(archive), str(stage)], check=True)
        app = stage / item['bundle']
        if not app.is_dir() or app.is_symlink():
            raise ValueError('Expected app at ZIP root: ' + item['bundle'])
        files = inventory(app)
        observed = bundle_info(app)
        receipt = {'archive_sha256': item['sha256'], 'observed': observed, 'files': files}
        (stage / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        stage.rename(prepared)
    return check(item)


def prepare():
    require_macos()
    with exclusive():
        return prepare_locked(specification())


def update():
    require_macos()
    with exclusive():
        item = latest_specification()
        _, archive, _ = locations(item)
        download(item['url'], archive, item['sha256'])
        app = prepare_locked(item, repair=True)
        record = {key: item[key] for key in ('sha256', 'checked_on', 'reported_release')}
        temporary = current_file().with_suffix('.tmp')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(current_file())
    print('Bavarian is up to date and ready. Next: ./tools/nbt-bavarian open', flush=True)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('update', 'prepare', 'check', 'open'))
    args = parser.parse_args()
    try:
        if args.command == 'update':
            update()
        elif args.command == 'prepare':
            prepare()
        else:
            app = check()
            if args.command == 'open':
                print('Use Enable SSH for temporary access. Install/Pair manage the NBTApps agent.', flush=True)
                subprocess.run(['/usr/bin/open', '-n', str(app)], check=True)
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException,
            subprocess.CalledProcessError) as error:
        print('Bavarian error:', error, file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
