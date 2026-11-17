import json
from pathlib import Path
import socket
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nbt_hsfz_trace import decode, summarize


def packet(seq, payload):
    ethernet = b"\0" * 12 + b"\x08\x00"
    ip = bytearray(20)
    ip[0], ip[9] = 0x45, 6
    ip[2:4] = struct.pack(">H", 40 + len(payload))
    ip[12:16] = socket.inet_aton("169.254.1.1")
    ip[16:20] = socket.inet_aton("169.254.1.2")
    tcp = bytearray(20)
    struct.pack_into(">HHI", tcp, 0, 12345, 6801, seq)
    tcp[12] = 0x50
    return ethernet + ip + tcp + payload


def pcap(packets):
    data = bytes.fromhex("d4c3b2a1") + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    for index, p in enumerate(packets):
        data += struct.pack("<IIII", 1, index, len(p), len(p)) + p
    return data


class TraceTests(unittest.TestCase):
    def test_sensitive_options_not_exported(self):
        secret = b"DO-NOT-EXPORT-SIGNATURE"
        record = summarize(bytes.fromhex("f4 63 31 01 fd 2f") + secret, 1)
        self.assertEqual(record["option_bytes"], len(secret))
        self.assertNotIn(secret.decode(), json.dumps(record))
        self.assertEqual(record["routine"], "0xfd2f")

    def test_fragmented_tcp_and_duplicate_retransmit(self):
        payload = bytes.fromhex("f4 63 22 f1 01")
        frame = struct.pack(">IH", len(payload), 1) + payload
        first, rest = frame[:7], frame[7:]
        report = decode(pcap([packet(100, first), packet(100, first), packet(107, rest)]))
        self.assertEqual(len(report["records"]), 1)
        self.assertEqual(report["records"][0]["did"], "0xf101")

    def test_gap_and_truncated_packet_fail(self):
        frame = struct.pack(">IH", 5, 1) + bytes.fromhex("f4 63 22 f1 01")
        with self.assertRaisesRegex(ValueError, "gap"):
            decode(pcap([packet(100, frame[:7]), packet(109, frame[7:])]))
        with self.assertRaises(ValueError):
            decode(pcap([packet(100, frame)])[:-1])

    def test_reject_pcapng(self):
        with self.assertRaises(ValueError):
            decode(bytes.fromhex("0a0d0d0a") + b"\0" * 30)


if __name__ == "__main__":
    unittest.main()
