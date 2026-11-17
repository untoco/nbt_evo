#!/usr/bin/env python3
"""Offline metadata-only decoding of classic Ethernet/IPv4 HSFZ PCAP.

Does not emit request options, signatures, challenges, VIN or raw response data.
Rejects TCP gaps/out-of-order payload instead of inventing missing bytes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct


def summarize(body, kind):
    record = {"kind": hex(kind), "length": len(body)}
    if kind != 1 or len(body) < 3:
        return record
    uds = body[2:]
    record.update(source_ecu=hex(body[0]), target_ecu=hex(body[1]),
                  service=hex(uds[0]), uds_length=len(uds))
    if uds[0] in (0x31, 0x71) and len(uds) >= 4:
        record.update(subfunction=hex(uds[1]), routine="0x" + uds[2:4].hex(),
                      option_bytes=len(uds) - 4)
    elif uds[0] in (0x22, 0x62) and len(uds) >= 3:
        record["did"] = "0x" + uds[1:3].hex()
    elif uds[0] == 0x7f and len(uds) >= 3:
        record.update(request_service=hex(uds[1]), nrc=hex(uds[2]))
    return record


def decode(data):
    magic = data[:4]
    if magic in (bytes.fromhex("d4c3b2a1"), bytes.fromhex("4d3cb2a1")):
        endian = "<"
    elif magic in (bytes.fromhex("a1b2c3d4"), bytes.fromhex("a1b23c4d")):
        endian = ">"
    else:
        raise ValueError("Expected classic PCAP, not PCAPNG")
    if len(data) < 24 or struct.unpack_from(endian + "I", data, 20)[0] != 1:
        raise ValueError("Expected Ethernet PCAP")
    fraction = 1e9 if magic in (bytes.fromhex("4d3cb2a1"), bytes.fromhex("a1b23c4d")) else 1e6
    at, flows, records = 24, {}, []
    while at < len(data):
        if at + 16 > len(data):
            raise ValueError("Truncated PCAP header")
        sec, sub, cap, original = struct.unpack_from(endian + "IIII", data, at)
        at += 16
        if cap > 1024 * 1024 or at + cap > len(data) or cap < original:
            raise ValueError("Truncated/oversized captured packet")
        packet = data[at:at + cap]
        at += cap
        if len(packet) < 14:
            raise ValueError("Truncated Ethernet packet")
        if packet[12:14] != b"\x08\x00":
            continue
        ip = packet[14:]
        if len(ip) < 20:
            raise ValueError("Truncated IPv4 header")
        ih, total = (ip[0] & 15) * 4, int.from_bytes(ip[2:4], "big")
        if ih < 20 or total > len(ip) or total < ih:
            raise ValueError("Invalid IPv4 length")
        if ip[9] != 6:
            continue
        if int.from_bytes(ip[6:8], "big") & 0x3fff:
            raise ValueError("Fragmented IPv4 unsupported")
        tcp = ip[ih:total]
        if len(tcp) < 20:
            raise ValueError("Truncated TCP header")
        sp, dp, seq = struct.unpack_from(">HHI", tcp)
        th = (tcp[12] >> 4) * 4
        if th < 20 or th > len(tcp):
            raise ValueError("Invalid TCP header length")
        payload = tcp[th:]
        if not payload or 6801 not in (sp, dp):
            continue
        key = (socket.inet_ntoa(ip[12:16]), sp, socket.inet_ntoa(ip[16:20]), dp)
        state = flows.setdefault(key, {"next": seq, "buffer": bytearray()})
        skip = state["next"] - seq
        if skip < 0:
            raise ValueError("TCP gap/out-of-order payload")
        chunk = payload[skip:]
        state["next"] += len(chunk)
        state["buffer"].extend(chunk)
        buf = state["buffer"]
        while len(buf) >= 6:
            n, kind = struct.unpack_from(">IH", buf)
            if n > 4096:
                raise ValueError("Oversized HSFZ frame")
            if len(buf) < n + 6:
                break
            record = summarize(bytes(buf[6:n + 6]), kind)
            record.update(direction="request" if dp == 6801 else "response",
                          timestamp=sec + sub / fraction)
            records.append(record)
            del buf[:n + 6]
    if any(x["buffer"] for x in flows.values()):
        raise ValueError("Incomplete HSFZ stream")
    return {"pcap_sha256": hashlib.sha256(data).hexdigest(),
            "scope": "offline HSFZ metadata only; sensitive payloads omitted",
            "records": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(decode(args.pcap.read_bytes()), indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
