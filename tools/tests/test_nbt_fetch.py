"""Acquisition/deployment boundary tests; no downloads, Docker runs or host installs."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import nbt_fetch as FETCH
from nbt_download import digest


class DownloadBoundary(unittest.TestCase):
    def test_missing_receipt_never_attempts_docker_or_download(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(FETCH, 'RECEIPT', Path(d) / 'missing.json'), \
                 patch.object(FETCH, 'image_info') as images, patch.object(FETCH, 'download') as download:
                with self.assertRaisesRegex(ValueError, 'First run'):
                    FETCH.check(['re'], {})
            images.assert_not_called()
            download.assert_not_called()

    def test_modified_dependency_rejected_before_docker(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / 'package.deb'
            data.write_bytes(b'original')
            files = {'package.deb': digest(data)}
            data.write_bytes(b'modified')
            with patch.object(FETCH, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'Missing/changed dependency'):
                    FETCH.validate_files(files)

    def test_receipt_cannot_reference_files_outside_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'repo').mkdir()
            private = root / 'private'
            private.write_bytes(b'data')
            with patch.object(FETCH, 'ROOT', root / 'repo'):
                with self.assertRaises(ValueError):
                    FETCH.validate_files({'../private': digest(private)})

    def test_changed_recipe_invalidates_readiness(self):
        with tempfile.TemporaryDirectory() as d:
            receipt = Path(d) / 'ready.json'
            receipt.write_text(json.dumps({'fingerprints': {'recipe': 'old'}}))
            with patch.object(FETCH, 'RECEIPT', receipt), patch.object(FETCH, 'fingerprints', return_value={'recipe': 'new'}), \
                 patch.object(FETCH, 'image_info') as images:
                with self.assertRaisesRegex(ValueError, 'recipes changed'):
                    FETCH.check(['re'], {})
            images.assert_not_called()

    def test_verified_receipt_performs_no_transfers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / 'artifact'
            data.write_bytes(b'data')
            receipt = root / 'ready.json'
            receipt.write_text(json.dumps({'fingerprints': {}, 'components': {'re': {
                'base': 'image@sha256:pinned', 'files': {'artifact': digest(data)}}}}))
            with patch.object(FETCH, 'ROOT', root), patch.object(FETCH, 'RECEIPT', receipt), \
                 patch.object(FETCH, 'fingerprints', return_value={}), \
                 patch.object(FETCH, 'bases', return_value={'re': ('image@sha256:pinned', 'arm64')}), \
                 patch.object(FETCH, 'image_info', return_value={'Architecture': 'arm64'}), \
                 patch.object(FETCH, 'download') as download, patch.object(FETCH, 'run') as run:
                FETCH.check(['re'], {})
            download.assert_not_called()
            run.assert_not_called()

    def test_apt_prefetch_mounts_only_public_recipes_and_dependency_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(FETCH, 'ROOT', root), patch.object(FETCH, 'CACHE', root / '.build/downloads'), \
                 patch.object(FETCH, 'run') as run, patch.object(FETCH.subprocess, 'run') as cleanup:
                FETCH.docker_dependency_step('dev', 'base@sha256:pinned', 'amd64', 'apt')
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--pull') + 1], 'never')
            self.assertIn('type=bind,src=' + str(root / 'tools/nbt-dev') + ',dst=/recipes,readonly', command)
            self.assertFalse(any('docker.sock' in str(x) for x in command))
            self.assertIn('NBT_APT_I386=1', command)
            name = command[command.index('--name') + 1]
            self.assertEqual(cleanup.call_args.args[0], ['docker', 'rm', '-f', name])

    def test_qnx_source_helper_refuses_network_in_offline_mode(self):
        helper = FETCH.load_helper('test_offline_sources', 'tools/nbt-dev/prepare-qnx-build.py')
        with tempfile.TemporaryDirectory() as d, patch.object(helper, 'CACHE', Path(d)), \
             patch.object(helper.urllib.request, 'urlopen') as request:
            with self.assertRaisesRegex(ValueError, 'Missing verified GNU source'):
                helper.fetch(helper.SOURCES[0], offline=True)
        request.assert_not_called()

    def test_debian_metadata_refuses_network_in_offline_mode(self):
        helper = FETCH.load_helper('test_offline_metadata', 'tools/nbt-dev/prepare-qnx-metadata.py')
        with tempfile.TemporaryDirectory() as d, patch.object(helper.urllib.request, 'urlopen') as request:
            with self.assertRaisesRegex(ValueError, 'Missing verified Debian index'):
                helper.fetch('https://example.invalid/InRelease', Path(d) / 'InRelease', offline=True)
        request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
