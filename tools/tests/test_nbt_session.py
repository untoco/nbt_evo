"""Offline workflow tests: fake gateway and SSH, no vehicle connections."""
import contextlib
import io
import json
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nbt_session as app

IFCONFIG = """en5: flags=8863<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 169.254.42.17 netmask 0xffff0000 broadcast 169.254.255.255
    status: active
en0: flags=8863<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.10.2 netmask 0xffffff00 broadcast 192.168.10.255
    status: active
"""
ELF = b"\x7fELF\x01\x01" + b"\x00" * 10 + struct.pack("<HH", 3, 40) + b"fixture"


def completed(stdout=b"", stderr=b"", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeSystem:
    def __init__(self):
        self.calls, self.probes = [], []
        self.peers = ["169.254.1.1"]
        self.route = "en5"
        self.discovered = False
        self.identified = False

    def run(self, args, timeout=30):
        self.calls.append(args)
        if args == ["ifconfig"]:
            return completed(IFCONFIG.encode())
        if args[0] == "route":
            return completed(f"interface: {self.route}\n".encode())
        raise AssertionError(args)

    def probe(self, address, port, timeout):
        self.probes.append((address, port))

    def discover(self, local_ip, broadcast, timeout):
        self.discovered = True
        return self.peers, "\n".join(f"{p}: b'test'" for p in self.peers)

    def identify(self, address, timeout):
        self.identified = True
        return [{"ecu": "HU", "address": 99, "did": 61831, "result": "ok", "value": "01 02"}]


class FakeSSH:
    instances = []
    bad_framework = False
    fail_framework = False
    platform = b"QNX nbt 6.5.0 build armle\n"
    firmware = b"NBTevo_N18092I\n"

    def __init__(self, config, address, system):
        self.commands, self.closed = [], False
        self.instances.append(self)

    def read(self, command, first=False):
        self.commands.append((command, first))
        if command == "uname -a":
            return completed(self.platform)
        if command == app.TEXT_READS["nbt-version"]:
            return completed(self.firmware)
        if command == app.TEXT_READS["display"]:
            return completed(b"resolution: x = 1280, y = 480\n")
        if command == app.TEXT_READS["cpu-info"]:
            return completed(b"ARMv7\n")
        if command.startswith("find /opt/hmi -name"):
            return completed()
        if command.startswith("find "):
            return completed(b"/usr/lib/libc.so.3\n/proc/boot/libc.so.3\n")
        if command.startswith("cat < "):
            if "libAppFramework" in command:
                if self.fail_framework:
                    return completed(ELF, b"connection lost", 255)
                if self.bad_framework:
                    return completed(b"banner before library" + ELF)
            return completed(ELF, b"server banner on stderr")
        raise AssertionError(command)

    def close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = json.loads(app.EXAMPLE.read_text())
        self.system = FakeSystem()
        FakeSSH.instances = []
        FakeSSH.bad_framework = FakeSSH.fail_framework = False
        FakeSSH.platform = b"QNX nbt 6.5.0 build armle\n"
        FakeSSH.firmware = b"NBTevo_N18092I\n"

    def tearDown(self):
        self.temporary.cleanup()

    def collect(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return app.collect(self.config, self.root, self.system, FakeSSH)

    def test_network_auto_uses_only_single_link_local_physical_interface(self):
        network = app.choose_network(self.config["network"], app.parse_interfaces(IFCONFIG))
        self.assertEqual(network, {"interface": "en5", "local_ip": "169.254.42.17",
                                   "broadcast": "169.254.255.255"})

    def test_multiple_link_local_interfaces_need_config(self):
        other = IFCONFIG.replace("192.168.10.2", "169.254.8.2")
        with self.assertRaisesRegex(ValueError, "network.interface"):
            app.choose_network(self.config["network"], app.parse_interfaces(other))

    def test_wifi_can_be_selected_without_link_local_address(self):
        self.config["network"]["interface"] = "en0"
        network = app.choose_network(self.config["network"], app.parse_interfaces(IFCONFIG))
        self.assertEqual(network["local_ip"], "192.168.10.2")
        self.assertEqual(network["broadcast"], "192.168.10.255")

    def test_config_rejects_password_and_remote_traversal(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"ssh": {"password": "do-not-save"}}))
        with self.assertRaisesRegex(ValueError, "ssh.password"):
            app.read_config(path)
        path.write_text(json.dumps({"capture": {"frameworks": {
            "libAppFramework.A.so": "/opt/../etc/file"}}}))
        with self.assertRaisesRegex(ValueError, "путь"):
            app.read_config(path)

    def test_init_is_minimal_and_never_overwrites_config(self):
        path = self.root / "local.json"
        arguments = ["nbt_session", "init", "--config", str(path)]
        with patch.object(sys, "argv", arguments), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(), 0)
            self.assertEqual(set(json.loads(path.read_text())["network"]), {"interface"})
            self.assertEqual(app.read_config(path), self.config)
            path.write_text('{"head_unit": {"ssh_ip": "169.254.9.9"}}\n')
            self.assertEqual(app.main(), 0)
        self.assertEqual(json.loads(path.read_text())["head_unit"]["ssh_ip"], "169.254.9.9")

    def test_gateway_is_never_reused_as_head_address(self):
        session, report = self.collect()
        self.assertEqual(report["result"], "partial")
        self.assertTrue(self.system.identified)
        self.assertFalse(FakeSSH.instances)
        self.assertTrue((session / "summary.json").is_file())
        self.assertIn("head_unit.ssh_ip", report["issues"][-1])

    def test_multiple_gateways_do_not_trigger_identification(self):
        self.system.peers.append("169.254.2.2")
        _, report = self.collect()
        self.assertFalse(self.system.identified)
        self.assertFalse(self.system.probes)
        self.assertEqual(len(report["gateway_candidates"]), 2)

    def test_wrong_route_blocks_tcp_and_diagnostic_reads(self):
        self.config["network"]["gateway_ip"] = "169.254.1.1"
        self.system.route = "utun4"
        _, report = self.collect()
        self.assertFalse(self.system.discovered)
        self.assertFalse(self.system.probes)
        self.assertFalse(self.system.identified)
        self.assertIn("Маршрут", report["issues"][0])

    def test_complete_capture_preserves_alias_paths_and_checksums(self):
        self.config["head_unit"]["ssh_ip"] = "169.254.9.9"
        session, report = self.collect()
        self.assertEqual(report["result"], "complete")
        self.assertEqual(len(report["files"]), 4)
        self.assertEqual(report["firmware"], "NBTevo_N18092I")
        self.assertEqual(report["firmware_source"], "ssh")
        self.assertEqual(report["display"], {"width": 1280, "height": 480})
        self.assertTrue((session / "config-used.json").is_file())
        self.assertEqual((session / "frameworks/libAppFramework.A.so").read_bytes(), ELF)
        self.assertTrue((session / "runtime/usr/lib/libc.so.3").is_file())
        self.assertTrue((session / "runtime/proc/boot/libc.so.3").is_file())
        self.assertEqual(len((session / "SHA256SUMS.txt").read_text().splitlines()), 4)
        self.assertTrue(FakeSSH.instances[0].closed)
        self.assertEqual(session.stat().st_mode & 0o777, 0o700)
        commands = [command for command, _ in FakeSSH.instances[0].commands]
        self.assertTrue(all(c.startswith(("uname ", "cat ", "find ", "pidin ", "/opt/sys/bin/"))
                            for c in commands))

    def test_failed_or_banner_corrupted_copy_is_not_published(self):
        self.config["head_unit"]["ssh_ip"] = "169.254.9.9"
        for attribute in ("fail_framework", "bad_framework"):
            with self.subTest(attribute=attribute):
                setattr(FakeSSH, attribute, True)
                session, report = self.collect()
                self.assertEqual(report["result"], "partial")
                self.assertFalse((session / "frameworks/libAppFramework.A.so").exists())
                self.assertFalse(list(session.rglob("*.part")))
                self.assertTrue(FakeSSH.instances[-1].closed)
                setattr(FakeSSH, attribute, False)

    def test_non_qnx_head_is_not_read_further(self):
        self.config["head_unit"]["ssh_ip"] = "169.254.9.9"
        FakeSSH.platform = b"Linux machine 6.6 x86_64\n"
        _, report = self.collect()
        self.assertEqual(len(FakeSSH.instances[0].commands), 1)
        self.assertEqual(report["files"], [])

    def test_closed_ssh_preserves_enet_results(self):
        self.config["head_unit"]["ssh_ip"] = "169.254.9.9"
        original_probe = self.system.probe
        def probe(address, port, timeout):
            if port == 22:
                raise ConnectionRefusedError("SSH port refused")
            original_probe(address, port, timeout)
        self.system.probe = probe
        session, report = self.collect()
        self.assertEqual(report["result"], "partial")
        self.assertTrue((session / "identify.json").is_file())
        self.assertFalse(FakeSSH.instances)

    def test_ssh_timeout_closes_master_and_keeps_summary(self):
        self.config["head_unit"]["ssh_ip"] = "169.254.9.9"
        class TimeoutSSH(FakeSSH):
            def read(self, command, first=False):
                if command.startswith("cat < "):
                    raise subprocess.TimeoutExpired("ssh", 1)
                return super().read(command, first)
        with contextlib.redirect_stdout(io.StringIO()):
            session, report = app.collect(self.config, self.root, self.system, TimeoutSSH)
        self.assertEqual(report["result"], "partial")
        self.assertTrue((session / "summary.json").is_file())
        self.assertTrue(FakeSSH.instances[-1].closed)

    def test_interrupted_capture_preserves_session_and_closes_ssh(self):
        self.config["head_unit"]["ssh_ip"] = "169.254.9.9"
        class InterruptedSSH(FakeSSH):
            def read(self, command, first=False):
                if command == app.TEXT_READS["display"]:
                    raise KeyboardInterrupt
                return super().read(command, first)
        with contextlib.redirect_stdout(io.StringIO()):
            session, report = app.collect(self.config, self.root, self.system, InterruptedSSH)
        self.assertTrue(report["interrupted"])
        self.assertTrue((session / "summary.json").is_file())
        self.assertTrue(FakeSSH.instances[-1].closed)

    def test_firmware_conflict_remains_visible_without_discarding_evidence(self):
        self.config["head_unit"] = {"ssh_ip": "169.254.9.9", "firmware": "NBTevo_V20153I"}
        _, report = self.collect()
        self.assertEqual(report["result"], "partial")
        self.assertEqual(report["firmware_entered"], "NBTevo_V20153I")
        self.assertEqual(report["firmware_detected"], "NBTevo_N18092I")
        self.assertEqual(len(report["files"]), 4)

    def test_ssh_quotes_paths_and_never_disables_host_verification(self):
        class RecordingSystem:
            def __init__(self):
                self.calls = []
                self.interactive = []
            def run(self, args, timeout, interactive=False):
                self.calls.append(args)
                self.interactive.append(interactive)
                return completed(ELF)
        system = RecordingSystem()
        ssh = app.SSH(self.config["ssh"], "169.254.9.9", system)
        try:
            ssh.read("uname -a", first=True)
            with tempfile.TemporaryDirectory() as tmp:
                app.save_binary(ssh, Path(tmp), "/opt/lib name's.so",
                                Path("framework.so"), "framework")
            self.assertIn("StrictHostKeyChecking=ask", system.calls[0])
            self.assertIn("StrictHostKeyChecking=yes", system.calls[1])
            self.assertEqual(system.interactive, [True, False])
            self.assertEqual(shlex.split(system.calls[1][-1]), ["cat", "<", "/opt/lib name's.so"])
            self.assertNotIn("StrictHostKeyChecking=no", str(system.calls))
        finally:
            ssh.close()

    def test_dry_run_has_no_system_or_vehicle_calls(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        with patch.object(sys, "argv", ["nbt_session", "collect", "--dry-run", "--config", str(path)]), \
             patch.object(app.System, "run", side_effect=AssertionError("must not run")), \
             patch.object(app.System, "probe", side_effect=AssertionError("must not probe")), \
             patch.object(app.System, "discover", side_effect=AssertionError("must not discover")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(), 0)
        self.assertFalse(json.loads(output.getvalue())["writes_to_hu"])


if __name__ == "__main__":
    unittest.main()
