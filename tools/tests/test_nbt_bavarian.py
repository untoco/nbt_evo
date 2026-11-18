"""Offline host-tool boundaries: no real client, downloads, launches or vehicle access."""
import json
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).parents[1]))
import nbt_bavarian as BAV
import nbt_fetch as FETCH
from nbt_download import digest


class BavarianTool(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bavarian fixture ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'fixture.zip'
        with zipfile.ZipFile(self.archive, 'w') as package:
            package.writestr('bavarian-ssh-client.app/Contents/Info.plist', plistlib.dumps({
                'CFBundleExecutable': 'client', 'CFBundleShortVersionString': '0.2.0',
                'CFBundleVersion': 'fixture'}))
            package.writestr('bavarian-ssh-client.app/Contents/MacOS/client', b'fixture, never execute')
        self.item = {'file': self.archive.name, 'sha256': digest(self.archive),
                     'bundle': 'bavarian-ssh-client.app', 'reported_release': '0.2.3',
                     'url': 'https://example.invalid/fixture.zip'}
        self.addCleanup(patch.stopall)
        patch.object(BAV, 'ROOT', self.root).start()
        patch.object(BAV, 'specification', return_value=self.item).start()
        patch.object(BAV.platform, 'system', return_value='Darwin').start()
        patch.object(BAV.platform, 'machine', return_value='arm64').start()
        target = BAV.locations(self.item)[1]
        target.parent.mkdir(parents=True)
        target.write_bytes(self.archive.read_bytes())

    def fake_mac_command(self, command, **kwargs):
        if command[0] == '/usr/bin/ditto':
            with zipfile.ZipFile(command[-2]) as package:
                package.extractall(command[-1])
            (Path(command[-1]) / self.item['bundle'] / 'Contents/MacOS/client').chmod(0o755)
        return subprocess.CompletedProcess(command, 0, stdout='arm64\n', stderr='')

    def prepare_fixture(self):
        with patch.object(BAV.subprocess, 'run', side_effect=self.fake_mac_command), \
             patch.object(BAV, 'download', side_effect=AssertionError('offline preparation')):
            return BAV.prepare()

    def test_prepare_records_observed_bundle_version_without_claiming_changelog_match(self):
        app = self.prepare_fixture()
        receipt = json.loads((app.parent / 'receipt.json').read_text())
        self.assertEqual(receipt['observed']['bundle_version'], '0.2.0')
        self.assertEqual(receipt['observed']['architectures'], ['arm64'])
        with patch.object(BAV.subprocess, 'run', side_effect=self.fake_mac_command):
            self.assertEqual(BAV.check(), app)

    def test_tampered_bundle_is_rejected_before_signature_check_or_open(self):
        app = self.prepare_fixture()
        (app / 'Contents/MacOS/client').write_bytes(b'changed')
        with patch.object(BAV.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'bundle changed'):
                BAV.check()
            run.assert_not_called()

    def test_changed_archive_is_rejected_before_extraction(self):
        BAV.locations(self.item)[1].write_bytes(b'truncated')
        with patch.object(BAV.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Missing/changed'):
                BAV.prepare()
            run.assert_not_called()

    def test_failed_codesign_does_not_publish_prepared_bundle(self):
        def run(command, **kwargs):
            if command[0] == '/usr/bin/codesign':
                raise subprocess.CalledProcessError(1, command)
            return self.fake_mac_command(command, **kwargs)
        with patch.object(BAV.subprocess, 'run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                BAV.prepare()
        self.assertFalse(BAV.locations(self.item)[2].exists())
        self.assertTrue(BAV.locations(self.item)[1].exists())

    def test_wrong_architecture_does_not_publish_prepared_bundle(self):
        def run(command, **kwargs):
            if command[0] == '/usr/bin/lipo':
                return subprocess.CompletedProcess(command, 0, stdout='x86_64\n')
            return self.fake_mac_command(command, **kwargs)
        with patch.object(BAV.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'does not support host'):
                BAV.prepare()
        self.assertFalse(BAV.locations(self.item)[2].exists())

    def test_fetch_passes_pinned_hash_to_resumable_downloader_and_never_opens_app(self):
        with patch.object(BAV, 'download') as download, patch.object(BAV.subprocess, 'run') as run:
            BAV.fetch({'download_attempts': 2})
            download.assert_called_once_with(self.item['url'], BAV.locations(self.item)[1],
                                             self.item['sha256'], settings={'download_attempts': 2})
            run.assert_not_called()

    def test_open_only_after_check_and_never_downloads_or_prepares(self):
        with patch.object(sys, 'argv', ['nbt-bavarian', 'open']), \
             patch.object(BAV, 'check', side_effect=ValueError('not prepared')), \
             patch.object(BAV, 'prepare') as prepare, patch.object(BAV, 'download') as download, \
             patch.object(BAV.subprocess, 'run') as run:
            with self.assertRaises(SystemExit) as error:
                BAV.main()
            self.assertEqual(error.exception.code, 2)
            prepare.assert_not_called()
            download.assert_not_called()
            run.assert_not_called()

    def test_bavarian_only_fetch_needs_no_docker_config_or_core_receipt(self):
        cache = self.root / 'downloads'
        receipt = cache / 'ready.json'
        cache.mkdir()
        receipt.write_text('preserve core readiness')
        with patch.object(sys, 'argv', ['nbt_fetch.py', 'fetch', '--components', 'bavarian']), \
             patch.object(FETCH, 'CACHE', cache), patch.object(FETCH, 'RECEIPT', receipt), \
             patch.object(FETCH.RE, 'config', side_effect=AssertionError('no Docker config')), \
             patch.object(FETCH.shutil, 'which', side_effect=lambda name: '/fake/curl' if name == 'curl' else None), \
             patch.object(BAV, 'fetch') as fetch:
            FETCH.main()
            fetch.assert_called_once_with({})
        self.assertEqual(receipt.read_text(), 'preserve core readiness')

    def test_archive_only_check_needs_no_core_receipt_or_subprocess(self):
        with patch.object(sys, 'argv', ['nbt_fetch.py', 'check', '--components', 'bavarian']), \
             patch.object(FETCH.RE, 'config', side_effect=AssertionError('no core config')), \
             patch.object(BAV.subprocess, 'run') as run:
            FETCH.main()
            run.assert_not_called()

    def test_unsafe_zip_paths_and_symlinks_rejected(self):
        for name, target in [('../escape', None), ('/absolute', None),
                             ('link', '../../escape'), ('link', '/outside')]:
            with self.subTest(name=name, target=target):
                archive = self.root / 'unsafe.zip'
                with zipfile.ZipFile(archive, 'w') as package:
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = (stat.S_IFLNK | 0o777) << 16 if target else 0
                    package.writestr(entry, target or 'data')
                with self.assertRaisesRegex(ValueError, 'Unsafe ZIP'):
                    BAV.validate_zip(archive)

    def test_zip_never_writes_through_directory_symlink(self):
        archive = self.root / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            entry = zipfile.ZipInfo('app/link')
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(entry, 'destination')
            package.writestr('app/link/payload', 'data')
        with self.assertRaisesRegex(ValueError, 'writes through symlink'):
            BAV.validate_zip(archive)


if __name__ == '__main__':
    unittest.main()
