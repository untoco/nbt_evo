#!/usr/bin/env python3
"""Offline BMW F-series ENET discovery and identification."""

import argparse
import ipaddress
import socket
import struct
import sys
import time


HEADER = struct.Struct(">IH")
TESTER = 0xF4
TIMEOUT = 3
READS = (
    (0x10, "ZGW", (0x100B,)),
    (0x63, "HU", (0xF187, 0xF189, 0xF101)),
    (0x60, "KOMBI", (0xF187, 0xF189, 0xF101)),
)


def ipv4(value):
    return str(ipaddress.IPv4Address(value))


def discover(local_ip, broadcast):
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind((local_ip, 0))
        udp.sendto(HEADER.pack(0, 0x11), (broadcast, 6811))
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            udp.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                data, (peer, _) = udp.recvfrom(4096)
            except socket.timeout:
                break
            if len(data) >= HEADER.size:
                size, kind = HEADER.unpack_from(data)
                if kind == 0x11 and size == len(data) - HEADER.size:
                    found[peer] = data[HEADER.size:]
    for peer, identity in sorted(found.items()):
        print(f"{peer}: {identity!r}")
    return bool(found)


def receive_exact(conn, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HSFZ response timed out")
        conn.settimeout(remaining)
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Gateway closed the connection")
        data.extend(chunk)
    return bytes(data)


def read_identifier(conn, target, identifier):
    did = identifier.to_bytes(2, "big")
    request = bytes((TESTER, target, 0x22)) + did
    conn.settimeout(TIMEOUT)
    conn.sendall(HEADER.pack(len(request), 1) + request)
    deadline = time.monotonic() + TIMEOUT
    while True:
        size, kind = HEADER.unpack(receive_exact(conn, HEADER.size, deadline))
        if size > 65535:
            raise ValueError(f"Invalid HSFZ payload length: {size}")
        body = receive_exact(conn, size, deadline)
        if kind in (2, 0x12):
            continue
        if kind != 1:
            raise ValueError(f"Unexpected HSFZ control word: 0x{kind:04X}")
        if body[:2] != bytes((target, TESTER)):
            continue
        uds = body[2:]
        if uds.startswith(b"\x62" + did):
            return uds[3:].hex(" ")
        if len(uds) == 3 and uds[:2] == b"\x7f\x22":
            if uds[2] != 0x78:
                return f"negative response 0x{uds[2]:02X}"
            continue
        raise ValueError(f"Unexpected UDS response: {uds.hex(' ')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discovery = commands.add_parser("discover")
    discovery.add_argument("--local-ip", required=True, type=ipv4)
    discovery.add_argument("--broadcast", required=True, type=ipv4)
    identity = commands.add_parser("identify")
    identity.add_argument("gateway_ip", type=ipv4)
    args = parser.parse_args()
    try:
        if args.command == "discover":
            if discover(args.local_ip, args.broadcast):
                return 0
            print("No ENET discovery response", file=sys.stderr)
            return 1
        failed = False
        for target, label, identifiers in READS:
            for identifier in identifiers:
                try:
                    with socket.create_connection((args.gateway_ip, 6801), TIMEOUT) as conn:
                        result = read_identifier(conn, target, identifier)
                except (OSError, ValueError) as error:
                    result = f"error: {error}"
                    failed = True
                print(f"{label} 0x{target:02X} DID 0x{identifier:04X}: {result}", flush=True)
        return int(failed)
    except OSError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
