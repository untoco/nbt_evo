"""Acquisition/deployment boundary tests; no downloads, Docker runs or host installs."""
import importlib.util
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import nbt_fetch as FETCH
from nbt_download import digest


class DownloadBoundary(unittest.TestCase):
    def test_fetch_qnx_invokes_preparation_with_only_supported_arguments(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'packages.json'
            cache = root / 'debs'
            cache.mkdir()
            package = cache / 'libcc1-0_10.2.1-6_arm64.deb'
            package.write_bytes(b'verified package')
            duplicate = cache / 'libcc1-0_10.2.1-6_arm64 2.deb'
            duplicate.write_bytes(b'unneeded placeholder')
            manifest.write_text(json.dumps({'packages': [{'filename': package.name}]}))
            deps = SimpleNamespace(MANIFEST=manifest, CACHE=cache,
                                   normalize=lambda packages: [])
            sources = SimpleNamespace(SOURCES=[], CACHE=root / 'sources')
            metadata = SimpleNamespace(SUITES=[], CACHE=root / 'metadata')
            with patch.object(FETCH, 'fetch_qnx_tree', return_value=root / 'tree'), \
                 patch.object(FETCH, 'load_helper', side_effect=[deps, sources, metadata]), \
                 patch.object(FETCH, 'qnx_compiler_inputs', return_value=[]), \
                 patch.object(FETCH, 'download') as download, \
                 patch.object(FETCH, 'run') as run:
                paths = FETCH.fetch_qnx({})
            self.assertEqual(paths, [package])
            with patch.object(FETCH, 'ROOT', root), \
                 patch.object(FETCH, 'digest', side_effect=lambda path: self.assertEqual(path, package) or digest(path)):
                inventory = FETCH.snapshot(paths)
            self.assertEqual(inventory, {'debs/' + package.name: digest(package)})
            self.assertTrue(duplicate.exists())
            run.assert_called_once_with([
                sys.executable, 'tools/nbt-dev/prepare-qnx-build.py',
                '--offline', '--platform', 'linux/arm64'])
            download.assert_not_called()

    def test_qnx_helpers_load_in_fresh_process_without_sibling_search_path(self):
        script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tools'))
import nbt_fetch
sources = nbt_fetch.load_helper('qnx_sources', 'tools/nbt-dev/prepare-qnx-build.py')
assert len(sources.SOURCES) == 2
inputs = nbt_fetch.load_helper('qnx_inputs', 'tools/nbt-dev/qnx_inputs.py')
assert callable(inputs.prepare_context)
assert not any(Path(p).name == 'nbt-dev' for p in sys.path)
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', script, str(FETCH.ROOT)],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, '-E', str(FETCH.ROOT / 'tools/nbt-dev/prepare-qnx-build.py'), '--help'],
                                    cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--offline', result.stdout)

    def test_qnx_required_dataless_input_fails_before_reading(self):
        from types import SimpleNamespace
        helper = FETCH.load_helper('qnx_required_test', 'tools/nbt-dev/qnx_inputs.py')
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            source = tree / 'Dockerfile'
            source.write_text('placeholder')
            result = subprocess.CompletedProcess([], 0, stdout=b'Dockerfile\0')
            with patch.object(helper.subprocess, 'run', return_value=result), \
                 patch.object(Path, 'is_symlink', return_value=False), \
                 patch.object(Path, 'stat', return_value=SimpleNamespace(st_flags=0x40000000)):
                with self.assertRaisesRegex(ValueError, 'Required QNX input.*Dockerfile'):
                    helper.compiler_inputs(tree)

    def test_qnx_inventory_and_context_exclude_untracked_duplicates(self):
        helper = FETCH.load_helper('qnx_context_test', 'tools/nbt-dev/qnx_inputs.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / 'tree'
            (tree / 'sdp/etc').mkdir(parents=True)
            original = tree / 'sdp/etc/named.conf'
            original.write_bytes(b'required')
            (tree / 'Dockerfile').write_text('FROM base')
            subprocess.run(['git', 'init', '-q', str(tree)], check=True)
            subprocess.run(['git', '-C', str(tree), 'add', '.'], check=True)
            duplicate = tree / 'sdp/etc/named 2.conf'
            duplicate.write_bytes(b'placeholder')
            destination = root / 'context'
            destination.mkdir()
            (destination / 'stale').write_text('previous context')
            real_copy = helper.shutil.copy2
            def guarded_copy(source, target):
                self.assertNotEqual(source, duplicate)
                return real_copy(source, target)
            with patch.object(helper.shutil, 'copy2', side_effect=guarded_copy), \
                 patch.object(helper.shutil, 'rmtree', side_effect=AssertionError('must not delete old context')):
                helper.prepare_context(tree, destination)
            self.assertEqual((destination / 'sdp/etc/named.conf').read_bytes(), b'required')
            self.assertFalse((destination / 'sdp/etc/named 2.conf').exists())
            self.assertFalse((destination / 'stale').exists())
            self.assertTrue(duplicate.exists())
            previous = list(root.glob('context.previous-*'))
            self.assertEqual(len(previous), 1)
            self.assertEqual((previous[0] / 'stale').read_text(), 'previous context')

    def test_qnx_context_copy_failure_preserves_previous(self):
        helper = FETCH.load_helper('qnx_copy_failure', 'tools/nbt-dev/qnx_inputs.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Dockerfile'
            source.write_text('new')
            destination = root / 'context'
            destination.mkdir()
            (destination / '.DS_Store').write_bytes(b'old')
            with patch.object(helper, 'compiler_inputs', return_value=[source]), \
                 patch.object(helper.shutil, 'copy2', side_effect=OSError('copy failed')):
                with self.assertRaisesRegex(OSError, 'copy failed'):
                    helper.prepare_context(root, destination)
            self.assertEqual((destination / '.DS_Store').read_bytes(), b'old')
            self.assertEqual(len(list(root.glob('context.staging-*'))), 1)

    def test_qnx_context_publish_failure_restores_previous(self):
        helper = FETCH.load_helper('qnx_publish_failure', 'tools/nbt-dev/qnx_inputs.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Dockerfile'
            source.write_text('new')
            destination = root / 'context'
            destination.mkdir()
            (destination / '.DS_Store').write_bytes(b'old')
            rename = Path.rename
            def fail_publish(path, target):
                if '.staging-' in path.name:
                    raise OSError('publish failed')
                return rename(path, target)
            with patch.object(helper, 'compiler_inputs', return_value=[source]), \
                 patch.object(Path, 'rename', fail_publish):
                with self.assertRaisesRegex(OSError, 'publish failed'):
                    helper.prepare_context(root, destination)
            self.assertEqual((destination / '.DS_Store').read_bytes(), b'old')
            self.assertFalse(list(root.glob('context.previous-*')))
            staging = list(root.glob('context.staging-*'))
            self.assertEqual((staging[0] / 'Dockerfile').read_text(), 'new')

    def test_debian_cache_does_not_read_unneeded_aliases(self):
        helper = FETCH.load_helper('deps_cache_test', 'tools/nbt-dev/qnx-deps.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache, downloads = root / 'cache', root / 'downloads'
            cache.mkdir()
            downloads.mkdir()
            (cache / 'fixture.deb').write_bytes(b'package')
            (downloads / 'fixture 2.deb').write_bytes(b'offloaded duplicate')
            package = {'filename': 'fixture.deb', 'size': 7, 'sha256': digest(cache / 'fixture.deb')}
            def guarded_hash(path):
                self.assertEqual(path.parent, cache)
                return digest(path)
            with patch.object(helper, 'CACHE', cache), patch.object(helper, 'DOWNLOADS', downloads), \
                 patch.object(helper, 'sha256', side_effect=guarded_hash):
                self.assertEqual(helper.normalize([package]), [])

    def test_debian_exact_name_avoids_offloaded_duplicate(self):
        helper = FETCH.load_helper('deps_exact_test', 'tools/nbt-dev/qnx-deps.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / 'downloads'
            downloads.mkdir()
            exact = downloads / 'fixture.deb'
            exact.write_bytes(b'package')
            (downloads / 'fixture 2.deb').write_bytes(b'package')
            package = {'filename': exact.name, 'size': 7, 'sha256': digest(exact)}
            def guarded_hash(path):
                self.assertNotIn(' 2.deb', path.name)
                return digest(path)
            with patch.object(helper, 'CACHE', root / 'cache'), patch.object(helper, 'DOWNLOADS', downloads), \
                 patch.object(helper, 'sha256', side_effect=guarded_hash):
                self.assertEqual(helper.normalize([package]), [])
            self.assertEqual((root / 'cache/fixture.deb').read_bytes(), b'package')

    def test_debian_alias_scan_skips_nonresident_candidates(self):
        helper = FETCH.load_helper('deps_alias_test', 'tools/nbt-dev/qnx-deps.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / 'downloads'
            downloads.mkdir()
            alias = downloads / 'browser-download.deb'
            alias.write_bytes(b'package')
            placeholder = downloads / 'fixture 2.deb'
            placeholder.write_bytes(b'package')
            package = {'filename': 'fixture.deb', 'size': 7, 'sha256': digest(alias)}
            def guarded_hash(path):
                self.assertNotEqual(path, placeholder)
                return digest(path)
            original = helper.resident
            with patch.object(helper, 'CACHE', root / 'cache'), patch.object(helper, 'DOWNLOADS', downloads), \
                 patch.object(helper, 'resident', side_effect=lambda p: p != placeholder and original(p)), \
                 patch.object(helper, 'sha256', side_effect=guarded_hash):
                self.assertEqual(helper.normalize([package]), [])
            self.assertEqual((root / 'cache/fixture.deb').read_bytes(), b'package')

    def test_dataless_flag_is_detected_without_opening_file(self):
        from unittest.mock import Mock
        helper = FETCH.load_helper('deps_flags_test', 'tools/nbt-dev/qnx-deps.py')
        candidate = Mock()
        candidate.is_file.return_value = True
        candidate.stat.return_value.st_flags = 0x40000000
        self.assertFalse(helper.resident(candidate))
        candidate.open.assert_not_called()
        candidate.stat.return_value.st_flags = 64
        self.assertTrue(helper.resident(candidate))

    def test_hash_timeout_reports_the_file(self):
        from nbt_download import digest as file_digest
        with patch.object(Path, 'open', side_effect=OSError(60, 'Operation timed out')):
            with self.assertRaisesRegex(OSError, 'fixture.deb'):
                file_digest(Path('fixture.deb'))

    def test_wheel_lock_matches_hashed_requirements(self):
        lock = FETCH.read_json(FETCH.ROOT / 'tools/nbt-dev/python-wheels.lock.json')
        requirements = (FETCH.ROOT / 'tools/nbt-dev/python-requirements.txt').read_text()
        self.assertEqual(len(lock['wheels']), 3)
        for item in lock['wheels']:
            self.assertIn('--hash=sha256:' + item['sha256'], requirements)
            self.assertTrue(item['url'].startswith('https://files.pythonhosted.org/'))
            self.assertEqual(item['url'].rsplit('/', 1)[1], item['file'])
            self.assertGreater(item['size'], 0)

    def test_wheel_transfer_resumes_and_checks_size_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            payload = b'complete wheel payload'
            source = cache / 'source'
            source.write_bytes(payload)
            filename = 'fixture.whl'
            lock = {'python': 'cp310', 'platform': 'linux/amd64', 'wheels': [{
                'file': filename, 'url': 'https://example.invalid/fixture.whl',
                'sha256': digest(source), 'size': len(payload)}]}
            partial = cache / 'dev/wheels/fixture.whl.part'
            partial.parent.mkdir(parents=True)
            partial.write_bytes(payload[:4])
            def transfer(command, **kwargs):
                self.assertIn('--continue-at', command)
                self.assertEqual(partial.read_bytes(), payload[:4])
                partial.write_bytes(payload)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(FETCH, 'CACHE', cache), patch.object(FETCH, 'read_json', return_value=lock), \
                 patch('nbt_download.subprocess.run', side_effect=transfer) as request:
                FETCH.fetch_wheels({'download_attempts': 1})
                FETCH.fetch_wheels({'download_attempts': 1})
            self.assertEqual(request.call_count, 1)
            self.assertEqual((partial.parent / filename).read_bytes(), payload)
            self.assertFalse(partial.exists())

    def test_corrupt_wheel_is_not_accepted_or_certified(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            lock = {'python': 'cp310', 'platform': 'linux/amd64', 'wheels': [{
                'file': 'fixture.whl', 'url': 'https://example.invalid/fixture.whl',
                'sha256': '0' * 64, 'size': 7}]}
            def transfer(command, **kwargs):
                Path(command[command.index('--output') + 1]).write_bytes(b'corrupt')
                return subprocess.CompletedProcess(command, 0)
            with patch.object(FETCH, 'CACHE', cache), patch.object(FETCH, 'read_json', return_value=lock), \
                 patch('nbt_download.subprocess.run', side_effect=transfer):
                with self.assertRaisesRegex(ValueError, 'incorrect sha256 or size'):
                    FETCH.fetch_wheels({'download_attempts': 1})
            self.assertFalse((cache / 'dev/wheels/fixture.whl').exists())
            self.assertFalse((cache / 'ready.json').exists())

    def test_platform_specific_manifest_is_selected_from_image_index(self):
        def inspect(command, **kwargs):
            # This Docker store returns empty platform fields for an OCI index.
            image = {'Os': '', 'Architecture': ''}
            if '--platform' in command:
                self.assertEqual(command[command.index('--platform') + 1], 'linux/amd64')
                image = {'Os': 'linux', 'Architecture': 'amd64', 'Id': 'sha256:platform-image'}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([image]), stderr='')
        with patch.object(FETCH.subprocess, 'run', side_effect=inspect):
            self.assertEqual(FETCH.image_info('image@sha256:index', 'amd64')['Id'], 'sha256:platform-image')

    def test_real_platform_mismatch_is_rejected(self):
        response = subprocess.CompletedProcess([], 0, stdout=json.dumps([{'Os': 'linux', 'Architecture': 'arm64'}]), stderr='')
        with patch.object(FETCH.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(ValueError, 'expected linux/amd64, got linux/arm64'):
                FETCH.image_info('image', 'amd64')

    def test_absent_platform_returns_missing_but_daemon_errors_do_not(self):
        with patch.object(FETCH.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stdout='', stderr='No such image')):
            self.assertIsNone(FETCH.image_info('image', 'amd64'))
        with patch.object(FETCH.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stdout='', stderr='Cannot connect to Docker daemon')):
            with self.assertRaisesRegex(ValueError, 'Cannot inspect linux/amd64'):
                FETCH.image_info('image', 'amd64')

    def test_compiler_inventory_does_not_read_unrelated_go_fixtures(self):
        helper = FETCH.load_helper('qnx_inventory_test', 'tools/nbt-dev/qnx_inputs.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / 'qnx'
            (tree / 'go/testdata').mkdir(parents=True)
            (tree / 'go/testdata/cloud-placeholder.pkg').write_bytes(b'not a compiler input')
            (tree / 'sdp').mkdir()
            (tree / 'sdp/libc.so').write_bytes(b'required')
            (tree / 'Dockerfile').write_text('FROM base')
            subprocess.run(['git', 'init', '-q', str(tree)], check=True)
            subprocess.run(['git', '-C', str(tree), 'add', 'Dockerfile', 'sdp'], check=True)
            def guarded_digest(path):
                if 'go' in Path(path).parts:
                    raise OSError('Resource deadlock avoided')
                return digest(path)
            with patch.object(FETCH, 'ROOT', root), patch.object(FETCH, 'digest', side_effect=guarded_digest), \
                 patch.object(FETCH, 'load_helper', return_value=helper):
                inventory = FETCH.snapshot(FETCH.qnx_compiler_inputs(tree))
            self.assertIn('qnx/sdp/libc.so', inventory)
            self.assertFalse(any('/go/' in name for name in inventory))

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
