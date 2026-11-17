import sys
from pathlib import Path
import struct
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nbt_ssh_relay_probe import (HsfzFilter, PolicyError, RelayDecoder,
                                 WsDecoder, check_hsfz, ws_send)


def frame(payload, kind=1):
    return struct.pack(">IH", len(payload), kind) + payload


class RelayTests(unittest.TestCase):
    def test_read_only_and_programming_routines_rejected(self):
        self.assertIn("f101", check_hsfz(frame(bytes.fromhex("f4 63 22 f1 01"))))
        for uds in ("11 01", "10 02", "27 11", "2e f1 90 00", "34 00", "36 01",
                    "31 01 fd ee 38", "31 01 10 0e", "31 01 fd 2e ff 00 00 00", "31 01 10 0e 00"):
            with self.subTest(uds=uds), self.assertRaises(PolicyError):
                check_hsfz(frame(bytes.fromhex("f4 63 " + uds)))

    def test_wrong_target_and_control_word(self):
        for packet in (frame(bytes.fromhex("f4 12 22 f1 01")), frame(b"", 0x40),
                       frame(bytes.fromhex("00 f4"), 0x13)):
            if packet[-2:] == b"\x00\xf4":
                self.assertIn("alive", check_hsfz(packet))
            else:
                with self.assertRaises(PolicyError):
                    check_hsfz(packet)

    def test_partial_hsfz_and_multiple_packets(self):
        data = frame(bytes.fromhex("f4 63 22 f1 01"))
        guard = HsfzFilter()
        self.assertEqual(guard.feed(data[:8]), [])
        self.assertEqual([x[0] for x in guard.feed(data[8:] + data)], [data, data])

    def test_unsafe_second_frame_is_not_returned(self):
        guard = HsfzFilter()
        data = frame(bytes.fromhex("f4 63 22 f1 01"))
        with self.assertRaises(PolicyError):
            guard.feed(data + frame(bytes.fromhex("f4 63 36 01")))

    def test_real_capture_log_kind_and_completion_layout(self):
        decoder = RelayDecoder()
        raw = bytes.fromhex("82 03 00 00 00 0a") + b"Gate: open" + b"\x83\x01"
        self.assertEqual(decoder.feed(raw[:4]), [])
        self.assertEqual(decoder.feed(raw[4:]), [("log", b"Gate: open"), ("completion", 1)])

    def test_unknown_84_is_captured_not_forwarded(self):
        self.assertEqual(RelayDecoder().feed(b"\x84\0\0\0\x02OK"),
                         [("unknown", b"\x84\0\0\0\x02OK")])

    def test_partial_ws_and_fragmented_message_with_ping(self):
        d = WsDecoder()
        self.assertEqual(d.feed(b"\x02\x03a"), [])
        self.assertEqual(d.feed(b"bc\x89\x01!\x80\x02de"), [(9, b"!"), (2, b"abcde")])

    def test_oversized_and_masked_server_ws_rejected(self):
        for packet in (b"\x82\xff" + struct.pack(">Q", 2**30), b"\x82\x80"):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                WsDecoder().feed(packet)

    def test_ws_client_mask_roundtrip(self):
        class Sink:
            def sendall(self, data):
                self.data = data
        sink = Sink()
        ws_send(sink, b"hello")
        self.assertEqual(sink.data[:2], b"\x82\x85")
        mask = sink.data[2:6]
        self.assertEqual(bytes(x ^ mask[i % 4] for i, x in enumerate(sink.data[6:])), b"hello")


if __name__ == "__main__":
    unittest.main()
