#!/usr/bin/env python3
"""Offline review probes for evocode 913d573; no sockets or vehicle access.

Usage: python3 THIS_FILE /path/to/evocode
Uses only temporary output and a fake transport. Findings are observations,
not tests of compatibility with the F21.
"""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from evocode import catalog, cli, uds  # noqa: E402


class FakeHsfz:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class DeniedUds:
    def __init__(self, *args):
        pass

    def read_vin(self):
        return "OFFLINE_FIXTURE"

    def read_did(self, did):
        raise uds.UdsError(0x22, 0x33)


result = {"date": "2026-11-16", "status": "measured",
          "scope": "offline mocks only; no vehicle or network",
          "upstream_commit": "913d5733e04019d4134f825b8fa0cc65782d02f7"}
with tempfile.TemporaryDirectory() as tmp:
    out = Path(tmp) / "backup.ncd"
    console = io.StringIO()
    with patch.object(cli, "Hsfz", FakeHsfz), patch.object(cli, "Uds", DeniedUds):
        with contextlib.redirect_stdout(console):
            rc = cli.cmd_read(SimpleNamespace(host="offline.invalid", target=0x63, out=str(out)))
    result["all_reads_security_denied"] = {
        "return_code": rc, "file_created": out.exists(),
        "console": console.getvalue().splitlines(),
        "conclusion": "cmd_read skips every UdsError, including NRC 0x33, and emits a zero-datagroup backup"}

class WrongDidTransport:
    def request(self, target, request):
        return bytes.fromhex("62 F1 90 41 42")

result["wrong_did_echo"] = {
    "requested": "F101", "response": "62 F1 90 41 42",
    "accepted_payload_hex": uds.Uds(WrongDidTransport()).read_did(0xF101).hex(),
    "conclusion": "read_did checks positive SID but not echoed DID"}
params = catalog.load(str(Path(sys.argv[1]) / "catalogs/cafd_00001ef6.json"))
result["catalog_version"] = {
    "shipped": "006.031.014", "f21_measured": "006.052.007",
    "loaded_parameters": len(params),
    "conclusion": "catalog.load returns parameters without CAFD/version metadata; caller must enforce compatibility"}
print(json.dumps(result, ensure_ascii=False, indent=2))
