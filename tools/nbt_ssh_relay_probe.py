#!/usr/bin/env python3
"""Inspect Bavarian relay with bounded, explicitly filtered HSFZ forwarding.

Default is offline policy checks. --run contacts the existing Bavarian service.
Protocol reference: mhmm-ok/BMW-Supra-iDrive5-6-Android-Auto/RELAY-PROTOCOL.md.
Raw captures stay private under .build; do not publish challenges or keys.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import ssl
import struct
import time
from datetime import datetime


class PolicyError(ValueError):
    pass


def check_hsfz(frame):
    if len(frame) < 6:
        raise PolicyError("Incomplete HSFZ header")
    size, kind = struct.unpack(">IH", frame[:6])
    if size != len(frame) - 6 or size > 4096:
        raise PolicyError("Invalid HSFZ length")
    body = frame[6:]
    if kind == 0x13 and body == b"\x00\xf4":
        return "HSFZ alive response"
    if kind != 1 or len(body) < 3 or body[:2] != b"\xf4\x63":
        raise PolicyError("Only HU 0x63 diagnostic traffic is allowed")
    uds = body[2:]
    if uds[0] == 0x22 and len(uds) == 3:
        return "HU read DID 0x" + uds[1:].hex()
    if uds == b"\x3e\x00":
        return "HU tester present"
    # No generic RoutineControl, SecurityAccess, reset, flash, writes or IO control.
    raise PolicyError("Unapproved HU request prefix " + uds[:4].hex(" "))


class HsfzFilter:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        approved = []
        while len(self.buffer) >= 6:
            size = int.from_bytes(self.buffer[:4], "big")
            if size > 4096:
                raise PolicyError("Oversized HSFZ frame")
            if len(self.buffer) < size + 6:
                break
            frame = bytes(self.buffer[:size + 6])
            approved.append((frame, check_hsfz(frame)))
            del self.buffer[:size + 6]
        return approved


class RelayDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while self.buffer:
            tag = self.buffer[0]
            if tag == 0x83:
                if len(self.buffer) < 2:
                    break
                result.append(("completion", self.buffer[1]))
                del self.buffer[:2]
            elif tag in (0x81, 0x82):
                offset = 1 if tag == 0x81 else 2
                if len(self.buffer) < offset + 4:
                    break
                n = int.from_bytes(self.buffer[offset:offset + 4], "big")
                if n > 1024 * 1024:
                    raise ValueError("Oversized relay message")
                end = offset + 4 + n
                if len(self.buffer) < end:
                    break
                payload = bytes(self.buffer[offset + 4:end])
                result.append(("car_write" if tag == 0x81 else "log", payload))
                del self.buffer[:end]
            else:
                result.append(("unknown", bytes(self.buffer)))
                self.buffer.clear()
                break
        return result


def length_field(tag, body):
    return bytes([tag]) + struct.pack(">I", len(body)) + body


def ws_send(sock, payload, opcode=2):
    n = len(payload)
    if n < 126:
        header = bytes([0x80 | opcode, 0x80 | n])
    elif n < 65536:
        header = bytes([0x80 | opcode, 0xfe]) + struct.pack(">H", n)
    else:
        header = bytes([0x80 | opcode, 0xff]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


class WsDecoder:
    def __init__(self):
        self.buffer = bytearray()
        self.fragments = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        out = []
        while len(self.buffer) >= 2:
            a, b = self.buffer[:2]
            if a & 0x70 or b & 0x80:
                raise ValueError("Unsupported compressed/masked server frame")
            n, off = b & 127, 2
            extra = 2 if n == 126 else (8 if n == 127 else 0)
            if len(self.buffer) < off + extra:
                break
            if extra:
                n = int.from_bytes(self.buffer[off:off + extra], "big")
                off += extra
            if n > 1024 * 1024:
                raise ValueError("Oversized WebSocket frame")
            if len(self.buffer) < off + n:
                break
            payload = bytes(self.buffer[off:off + n])
            del self.buffer[:off + n]
            op = a & 15
            if op in (8, 9, 10):
                if not a & 0x80 or n > 125:
                    raise ValueError("Invalid WebSocket control frame")
                out.append((op, payload))
            elif op in (0, 2):
                self.fragments.extend(payload)
                if len(self.fragments) > 1024 * 1024:
                    raise ValueError("Oversized fragmented message")
                if a & 0x80:
                    out.append((2, bytes(self.fragments)))
                    self.fragments.clear()
            else:
                raise ValueError("Unexpected WebSocket opcode")
        return out


def connect_relay():
    host = "pretzel.bavarian-aa.uk"
    raw = socket.create_connection((host, 8443), timeout=6)
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET / HTTP/1.1\r\nHost: {host}:8443\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(request.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            header += sock.recv(4096)
            if len(header) > 16384 or not header:
                raise ValueError("Invalid WebSocket HTTP response")
        head, rest = header.split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        fields = {k.lower(): v.strip() for k, v in fields.items()}
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if lines[0].split()[1] != "101" or fields.get("sec-websocket-accept") != accept:
            raise ValueError("WebSocket upgrade not accepted")
        return sock, rest
    except Exception:
        sock.close()
        raise


def run(gateway, local_ip):
    folder = Path(".build/nbt-qnx/access/relay") / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    events, capture = [], []
    report = {"gateway": gateway, "local_ip": local_ip, "events": events,
              "policy": "HU reads and tester present only; all RoutineControl blocked"}
    car = server = None
    try:
        car = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        car.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex("en5"))
        car.bind((local_ip, 0))
        car.settimeout(4)
        car.connect((gateway, 6801))
        server, initial = connect_relay()
        for payload in (length_field(6, b"0.1.0"), b"\x01" + b"\0" * 12,
                        length_field(2, b"ssh_enable")):
            ws_send(server, payload)
        ws, relay, guard = WsDecoder(), RelayDecoder(), HsfzFilter()
        deadline, writes, pending = time.monotonic() + 45, 0, initial
        done = False
        while not done and time.monotonic() < deadline:
            ready = [server] if pending or server.pending() else select.select([car, server], [], [], min(1, max(0, deadline - time.monotonic())))[0]
            for stream in ready:
                data = pending if pending and stream is server else stream.recv(65536)
                pending = b""
                if not data:
                    events.append({"event": "closed", "peer": "server" if stream is server else "car"})
                    done = True
                    break
                if stream is car:
                    capture.append({"direction": "car_to_server", "hex": data.hex()})
                    ws_send(server, length_field(3, data))
                    continue
                for opcode, payload in ws.feed(data):
                    if opcode == 9:
                        ws_send(server, payload, 10)
                    elif opcode == 8:
                        done = True
                    elif opcode == 2:
                        capture.append({"direction": "server_to_client", "hex": payload.hex()})
                        for kind, value in relay.feed(payload):
                            if kind == "car_write":
                                for frame, description in guard.feed(value):
                                    writes += 1
                                    if writes > 64:
                                        raise PolicyError("Request limit")
                                    time.sleep(0.05)
                                    car.sendall(frame)
                                    events.append({"event": "forwarded", "description": description})
                            elif kind == "unknown":
                                events.append({"event": "unknown_server_tag", "tag": hex(value[0]), "bytes": len(value)})
                                done = True
                            elif kind == "completion":
                                events.append({"event": kind, "code": value})
                                done = True
                            else:
                                # Private logs may contain challenges; don't print them.
                                events.append({"event": "server_log", "bytes": len(value)})
        report["result"] = "stopped" if done else "deadline"
    except (OSError, ValueError) as exc:
        report["result"] = "policy_stop" if isinstance(exc, PolicyError) else "error"
        report["error"] = str(exc)
    finally:
        for sock in (car, server):
            if sock:
                sock.close()
        for name, value in (("summary.json", report), ("private-capture.json", capture)):
            path = folder / name
            path.write_text(json.dumps(value, indent=2) + "\n")
            path.chmod(0o600)
    print(json.dumps({"folder": str(folder), **report}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", action="store_true")
    p.add_argument("--gateway", type=ipaddress.IPv4Address)
    p.add_argument("--local-ip", type=ipaddress.IPv4Address)
    a = p.parse_args()
    if not a.run:
        print("Offline mode. Run unit tests before --run. No connections made.")
    elif not a.gateway or not a.local_ip or not a.gateway.is_link_local or not a.local_ip.is_link_local:
        p.error("--run needs confirmed ENET --gateway and --local-ip link-local addresses")
    else:
        run(str(a.gateway), str(a.local_ip))
