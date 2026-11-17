"""Boundary checks for local analysis isolation, verification, and cleanup."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("nbt_re", Path(__file__).parents[1] / "nbt_re.py")
RE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RE)


class AnalysisBoundaries(unittest.TestCase):
    def test_config_cannot_expose_arbitrary_host_directory(self):
        cfg = json.loads(RE.EXAMPLE.read_text())
        cfg["data_dir"] = "/Users"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps(cfg))
            with self.assertRaisesRegex(ValueError, "ignored .build"):
                RE.config(p)

    def test_modified_archive_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "artifacts").mkdir()
            (root / "artifacts/a").write_bytes(b"changed")
            lock = root / "lock.json"
            lock.write_text(json.dumps({"a": {"file": "a", "sha256": "0" * 64}}))
            with patch.object(RE, "LOCK", lock), self.assertRaises(ValueError):
                RE.verify({"data": root})

    def test_offline_readonly_input_and_timeout_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "reports").mkdir()
            target = root / "sample"
            target.write_bytes(b"original")
            cfg = json.loads(RE.EXAMPLE.read_text())
            cfg["data"] = root
            calls = []

            def run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[:2] == ["docker", "run"]:
                    raise subprocess.TimeoutExpired(cmd, 1)
                return subprocess.CompletedProcess(cmd, 0, stdout="sha256:test\n")

            with patch.object(RE.subprocess, "run", side_effect=run):
                code = RE.run_container(cfg, ["rea", "analyze", "/input/binary"], target)
            self.assertEqual(code, 124)
            docker = calls[0]
            self.assertEqual(docker[docker.index("--network") + 1], "none")
            self.assertIn(f"type=bind,src={target},dst=/input/binary,readonly", docker)
            self.assertFalse(any("docker.sock" in x for x in docker))
            name = docker[docker.index("--name") + 1]
            self.assertIn(["docker", "rm", "-f", name], calls)
            self.assertEqual(target.read_bytes(), b"original")
            record = json.loads(next((root / "reports").glob("*/session.json")).read_text())
            self.assertTrue(record["timeout"])
            self.assertEqual(record["sha256"], RE.digest(target))
            self.assertTrue(record["input_unchanged"])

    def test_mcp_inputs_are_readonly_and_protocol_uses_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = json.loads(RE.EXAMPLE.read_text())
            cfg["data"] = Path(d)
            with patch.object(RE.subprocess, "call", return_value=0) as call, \
                    patch.object(RE.subprocess, "run"):
                self.assertEqual(RE.run_container(cfg, ["rea", "mcp"], stream=True), 0)
            cmd = call.call_args.args[0]
            self.assertIn("-i", cmd)
            self.assertIn(f"type=bind,src={Path(d) / 'inputs'},dst=/state/inputs,readonly", cmd)
            self.assertEqual(cmd[cmd.index("--network") + 1], "none")




class ArtifactDownloads(unittest.TestCase):
    def fixture(self, directory):
        import hashlib
        root = Path(directory)
        (root / 'artifacts').mkdir()
        data = b'complete artifact contents'
        lock = root / 'lock.json'
        lock.write_text(json.dumps({'fixture': {'file': 'test.zip', 'url': 'https://example.invalid/test.zip',
                                              'sha256': hashlib.sha256(data).hexdigest()}}))
        return root, lock, data, root / 'artifacts/test.zip', root / 'artifacts/test.zip.part'

    def test_timeout_resumes_and_uses_configured_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root, lock, data, final, partial = self.fixture(directory)
            partial.write_bytes(data[:4])
            calls = []

            def run(cmd):
                calls.append(cmd)
                self.assertEqual(cmd[cmd.index('--max-time') + 1], '7200')
                self.assertEqual(cmd[cmd.index('--continue-at') + 1], '-')
                if len(calls) == 1:
                    self.assertEqual(partial.read_bytes(), data[:4])
                    partial.write_bytes(data[:9])
                    return subprocess.CompletedProcess(cmd, 28)
                self.assertEqual(partial.read_bytes(), data[:9])
                partial.write_bytes(data)
                return subprocess.CompletedProcess(cmd, 0)

            with patch.object(RE, 'LOCK', lock), patch.object(RE.subprocess, 'run', side_effect=run):
                RE.prepare({'data': root, 'download_timeout_seconds': 7200})
            self.assertEqual(final.read_bytes(), data)
            self.assertFalse(partial.exists())

    def test_completed_wget_partial_requires_no_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root, lock, data, final, partial = self.fixture(directory)
            partial.write_bytes(data)
            with patch.object(RE, 'LOCK', lock), patch.object(RE.subprocess, 'run') as run:
                RE.prepare({'data': root})
            run.assert_not_called()
            self.assertEqual(final.read_bytes(), data)

    def test_failed_transfer_retains_data_and_reports_download_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root, lock, data, final, partial = self.fixture(directory)
            partial.write_bytes(data[:4])
            with patch.object(RE, 'LOCK', lock), patch.object(RE.subprocess, 'run', return_value=subprocess.CompletedProcess([], 28)):
                with self.assertRaisesRegex(ValueError, 'incomplete transfer.*4 bytes retained'):
                    RE.prepare({'data': root, 'download_attempts': 1})
            self.assertEqual(partial.read_bytes(), data[:4])
            self.assertFalse(final.exists())

    def test_wrong_hash_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, lock, data, final, partial = self.fixture(directory)
            partial.write_bytes(b'corrupt')
            with patch.object(RE, 'LOCK', lock), patch.object(RE.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
                with self.assertRaisesRegex(ValueError, 'incorrect sha256'):
                    RE.prepare({'data': root, 'download_attempts': 1})
            self.assertFalse(final.exists())
            self.assertFalse(partial.exists())

    def test_refused_range_restarts_without_appending_corrupt_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root, lock, data, final, partial = self.fixture(directory)
            partial.write_bytes(data[:4])
            calls = []

            def run(cmd):
                calls.append(cmd)
                if len(calls) == 1:
                    return subprocess.CompletedProcess(cmd, 33)
                self.assertFalse(partial.exists())
                partial.write_bytes(data)
                return subprocess.CompletedProcess(cmd, 0)

            with patch.object(RE, 'LOCK', lock), patch.object(RE.subprocess, 'run', side_effect=run):
                RE.prepare({'data': root})
            self.assertEqual(final.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
