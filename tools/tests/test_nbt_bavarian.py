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
                     'checked_on': '2026-11-18',
                     'url': 'https://example.invalid/fixture.zip'}
        self.addCleanup(patch.stopall)
        patch.object(BAV, 'ROOT', self.root).start()
        lock = self.root / 'fixture.lock.json'
        lock.write_text(json.dumps(self.item))
        patch.object(BAV, 'LOCK', lock).start()
        patch.object(BAV.platform, 'system', return_value='Darwin').start()
        patch.object(BAV.platform, 'machine', return_value='arm64').start()
        target = BAV.locations(self.item)[1]
        target.parent.mkdir(parents=True)
        target.write_bytes(self.archive.read_bytes())

    def fake_mac_command(self, command, **kwargs):
        if command[0] == 'curl':
            return subprocess.CompletedProcess(command, 0,
                                               stdout=f'{self.item["sha256"]}  {self.item["file"]}\n')
        if command[0] == '/usr/bin/ditto':
            with zipfile.ZipFile(command[-2]) as package:
                package.extractall(command[-1])
            (Path(command[-1]) / self.item['bundle'] / 'Contents/MacOS/client').chmod(0o755)
        return subprocess.CompletedProcess(command, 0, stdout='arm64\n', stderr='')

    def prepare_fixture(self):
        with patch.object(BAV.subprocess, 'run', side_effect=self.fake_mac_command), \
             patch.object(BAV, 'download', side_effect=AssertionError('offline preparation')):
            return BAV.prepare()

    def test_metadata_cleanup_preserves_quarantine_and_skips_symlink_targets(self):
        app = self.root / 'metadata.app'
        app.mkdir()
        binary = app / 'binary'
        binary.write_bytes(b'unchanged')
        outside = self.root / 'outside'
        outside.write_bytes(b'outside')
        (app / 'link').symlink_to(outside)
        deleted = []

        def run(command, **kwargs):
            if '-d' in command:
                deleted.append((command[-2], command[-1]))
                return subprocess.CompletedProcess(command, 0)
            self.assertNotEqual(command[-1], str(app / 'link'))
            return subprocess.CompletedProcess(command, 0, stdout=(
                'com.apple.FinderInfo\ncom.apple.ResourceFork\n'
                'com.apple.quarantine\ncom.apple.provenance\n'))

        with patch.object(BAV.subprocess, 'run', side_effect=run):
            BAV.clean_bundle_metadata(app)
        self.assertEqual(deleted, [(attribute, str(path)) for path in (app, binary)
                                  for attribute in ('com.apple.FinderInfo', 'com.apple.ResourceFork')])
        self.assertEqual(binary.read_bytes(), b'unchanged')
        self.assertEqual(outside.read_bytes(), b'outside')

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

    def test_update_reuses_verified_archive_and_bundle_then_selects_it(self):
        app = self.prepare_fixture()
        with patch.object(BAV.subprocess, 'run', side_effect=self.fake_mac_command) as run, \
             patch.object(BAV, 'download') as download:
            self.assertEqual(BAV.update(), app)
        self.assertNotIn('/usr/bin/ditto', [call.args[0][0] for call in run.call_args_list])
        self.assertIn('curl', [call.args[0][0] for call in run.call_args_list])
        self.assertEqual(download.call_args.args[2], self.item['sha256'])
        self.assertEqual(json.loads(BAV.current_file().read_text())['sha256'], self.item['sha256'])

    def test_update_new_release_pins_hash_locally_and_preserves_old_version(self):
        old_app = self.prepare_fixture()
        with zipfile.ZipFile(self.archive, 'a') as package:
            package.writestr('bavarian-ssh-client.app/Contents/new-resource', 'new release')
        new_sha = digest(self.archive)
        def run(command, **kwargs):
            if command[0] == 'curl':
                return subprocess.CompletedProcess(command, 0, stdout=f'{new_sha} *{self.item["file"]}\n')
            return self.fake_mac_command(command, **kwargs)
        def acquire(url, destination, expected):
            self.assertEqual(expected, new_sha)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.archive.read_bytes())
        with patch.object(BAV.subprocess, 'run', side_effect=run), \
             patch.object(BAV, 'download', side_effect=acquire):
            new_app = BAV.update()
            self.assertEqual(BAV.check(), new_app)
        self.assertTrue(old_app.exists())
        self.assertNotEqual(new_app, old_app)
        self.assertEqual(BAV.specification()['sha256'], new_sha)
        self.assertIsNone(BAV.specification()['reported_release'])
        self.assertEqual(json.loads(BAV.LOCK.read_text())['sha256'], self.item['sha256'])

    def test_failed_update_leaves_selected_version_unchanged(self):
        self.prepare_fixture()
        selected = {'sha256': self.item['sha256'], 'checked_on': '2026-11-18'}
        BAV.current_file().write_text(json.dumps(selected))
        before = BAV.current_file().read_bytes()
        with patch.object(BAV.subprocess, 'run', side_effect=self.fake_mac_command), \
             patch.object(BAV, 'download', side_effect=ValueError('download unavailable')):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                BAV.update()
        self.assertEqual(BAV.current_file().read_bytes(), before)

    def test_update_repairs_tampered_bundle_and_keeps_evidence(self):
        app = self.prepare_fixture()
        (app / 'Contents/MacOS/client').write_bytes(b'tampered')
        with patch.object(BAV.subprocess, 'run', side_effect=self.fake_mac_command), \
             patch.object(BAV, 'download'):
            self.assertEqual(BAV.update(), app)
        previous = list(app.parent.parent.glob('prepared.previous-*'))
        self.assertEqual(len(previous), 1)
        self.assertEqual((previous[0] / self.item['bundle'] / 'Contents/MacOS/client').read_bytes(), b'tampered')
        self.assertEqual((app / 'Contents/MacOS/client').read_bytes(), b'fixture, never execute')

    def test_missing_or_ambiguous_publisher_checksum_does_not_download(self):
        for index in ['<html>unavailable</html>', '0' * 64 + ' another.zip',
                      (self.item['sha256'] + ' ' + self.item['file'] + '\n') * 2]:
            with self.subTest(index=index), \
                 patch.object(BAV.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=index)), \
                 patch.object(BAV, 'download') as download:
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    BAV.update()
                download.assert_not_called()
        self.assertFalse(BAV.current_file().exists())


if __name__ == '__main__':
    unittest.main()
