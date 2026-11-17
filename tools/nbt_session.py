#!/usr/bin/env python3
"""Config-driven ENET reads and read-only SSH capture, using Python stdlib."""
import argparse
import contextlib
from datetime import datetime
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

import enet_identify as enet

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/nbt/session.example.json"
LOCAL = ROOT / "config/nbt/session.local.json"
FIRMWARE = re.compile(r"\bNBTevo_[A-Z][0-9]{5}[A-Z]\b", re.I)
TIMEZONE = ZoneInfo("Europe/Moscow")
TEXT_READS = {
    "uname": "uname -a",
    "nbt-version": "cat /opt/sys/etc/nbt_version.txt",
    "display": "/opt/sys/bin/LayerManagerControl get screen 0",
    "cpu-info": "pidin info",
}


def remote_path(value):
    if (not isinstance(value, str) or not value.startswith("/")
            or ".." in PurePosixPath(value).parts
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f"Недопустимый путь на HU: {value!r}")
    return value


def read_config(path):
    defaults = json.loads(EXAMPLE.read_text())
    supplied = json.loads(path.read_text())

    def merge(target, incoming, prefix=""):
        if not isinstance(incoming, dict):
            raise ValueError(f"Ожидался объект конфига: {prefix or 'root'}")
        for key, value in incoming.items():
            if key not in target:
                raise ValueError(f"Неизвестное поле {prefix + key}; пароль в конфиг не сохраняется")
            if isinstance(target[key], dict):
                merge(target[key], value, prefix + key + ".")
            else:
                target[key] = value

    merge(defaults, supplied)
    for section, fields in (("network", ("interface", "local_ip", "broadcast", "gateway_ip")),
                            ("head_unit", ("ssh_ip", "firmware")), ("ssh", ("user",))):
        for key in fields:
            value = defaults[section][key]
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise ValueError(f"Некорректное поле {section}.{key}")
    for section, key in (("network", "local_ip"), ("network", "broadcast"),
                         ("network", "gateway_ip"), ("head_unit", "ssh_ip")):
        if defaults[section][key]:
            ipaddress.IPv4Address(defaults[section][key])
    for section, key, limit in (("network", "timeout_seconds", 30),
                                ("ssh", "port", 65535), ("ssh", "connect_timeout", 60),
                                ("ssh", "command_timeout", 600)):
        value = defaults[section][key]
        if type(value) is not int or not 1 <= value <= limit:
            raise ValueError(f"Недопустимое значение {section}.{key}")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", defaults["ssh"]["user"]):
        raise ValueError("Недопустимое имя SSH пользователя")
    if type(defaults["ssh"]["legacy"]) is not bool:
        raise ValueError("ssh.legacy должен быть true или false")
    for value in defaults["capture"]["frameworks"].values():
        remote_path(value)
    directories = defaults["capture"]["runtime_search_dirs"]
    if not isinstance(directories, list) or not directories:
        raise ValueError("Нужен непустой список runtime_search_dirs")
    for value in directories:
        remote_path(value)
    for value in defaults["capture"]["runtime_files"].values():
        if value:
            remote_path(value)
        elif value != "":
            raise ValueError("Runtime-путь должен быть строкой")
    if not isinstance(defaults["output_dir"], str) or not defaults["output_dir"]:
        raise ValueError("Нужен output_dir")
    return defaults


def parse_interfaces(text):
    interfaces = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^([a-zA-Z0-9_.:-]+):\s+flags=", line)
        if match:
            current = match.group(1)
            interfaces[current] = {"active": None, "ipv4": []}
        if current is None:
            continue
        if "status: active" in line:
            interfaces[current]["active"] = True
        elif "status: inactive" in line:
            interfaces[current]["active"] = False
        match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+).*?\bnetmask (\S+)", line)
        if match:
            mask = match.group(2)
            if mask.startswith("0x"):
                mask = str(ipaddress.IPv4Address(int(mask, 16)))
            subnet = ipaddress.IPv4Network(f"{match.group(1)}/{mask}", strict=False)
            broadcast = re.search(r"\bbroadcast (\d+\.\d+\.\d+\.\d+)", line)
            interfaces[current]["ipv4"].append({
                "local_ip": match.group(1),
                "broadcast": broadcast.group(1) if broadcast else str(subnet.broadcast_address),
            })
    return interfaces


def choose_network(config, interfaces):
    interface = config["interface"]
    if not interface:
        candidates = [
            name for name, data in interfaces.items()
            if data["active"] is not False and name.startswith("en")
            and any(ipaddress.IPv4Address(item["local_ip"]).is_link_local
                    for item in data["ipv4"])
        ]
        if len(candidates) != 1:
            available = ", ".join(
                f"{name} ({','.join(x['local_ip'] for x in data['ipv4'])})"
                for name, data in interfaces.items()
                if name.startswith("en") and data["ipv4"] and data["active"] is not False
            )
            raise ValueError("Укажите network.interface в конфиге. Интерфейсы: " + available)
        interface = candidates[0]
    if interface not in interfaces or interfaces[interface]["active"] is False:
        raise ValueError(f"Интерфейс {interface} отсутствует или неактивен")
    addresses = interfaces[interface]["ipv4"]
    if not addresses:
        raise ValueError(f"На {interface} пока нет IPv4. Проверьте соединение и дождитесь получения адреса")
    if config["local_ip"]:
        addresses = [item for item in addresses if item["local_ip"] == config["local_ip"]]
        if not addresses:
            raise ValueError(f"network.local_ip не принадлежит интерфейсу {interface}")
    if len(addresses) != 1:
        raise ValueError(f"Укажите network.local_ip: на {interface} нет единственного IPv4")
    return {"interface": interface, "local_ip": addresses[0]["local_ip"],
            "broadcast": config["broadcast"] or addresses[0]["broadcast"]}


class System:
    def run(self, args, timeout=30, interactive=False):
        result = subprocess.run(args, stdout=subprocess.PIPE,
                                stderr=None if interactive else subprocess.PIPE,
                                timeout=timeout)
        if result.stderr is None:
            result.stderr = b""
        return result

    def probe(self, address, port, timeout):
        with socket.create_connection((address, port), timeout):
            pass

    def discover(self, local_ip, broadcast, timeout):
        enet.TIMEOUT = timeout
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            enet.discover(local_ip, broadcast)
        text = output.getvalue()
        peers = []
        for line in text.splitlines():
            address = line.partition(":")[0]
            peers.append(str(ipaddress.IPv4Address(address)))
        return peers, text

    def identify(self, address, timeout):
        enet.TIMEOUT = timeout
        records = []
        for target, label, identifiers in enet.READS:
            for identifier in identifiers:
                try:
                    with socket.create_connection((address, 6801), timeout) as conn:
                        value = enet.read_identifier(conn, target, identifier)
                    outcome = "negative" if value.startswith("negative response") else "ok"
                except (OSError, ValueError) as error:
                    value, outcome = str(error), "error"
                records.append({"ecu": label, "address": target, "did": identifier,
                                "result": outcome, "value": value})
        return records


class SSH:
    def __init__(self, config, address, system):
        self.config, self.system = config, system
        self.destination = f"{config['user']}@{address}"
        self.directory = tempfile.TemporaryDirectory(prefix="nbt-ssh-", dir="/tmp")
        self.control = Path(self.directory.name) / "socket"
        self.options = ["ssh", "-T", "-p", str(config["port"]),
                        "-o", f"ConnectTimeout={config['connect_timeout']}",
                        "-o", "ControlMaster=auto", "-o", "ControlPersist=30",
                        "-o", f"ControlPath={self.control}"]
        if config["legacy"]:
            self.options += [
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
                "-o", "Ciphers=+aes128-cbc,aes256-cbc,3des-cbc",
                "-o", "MACs=+hmac-sha1,hmac-sha1-96",
            ]

    def read(self, command, first=False):
        result = self.system.run(
            self.options + ["-o", "StrictHostKeyChecking=" + ("ask" if first else "yes"),
                            self.destination, command],
            timeout=self.config["command_timeout"],
            interactive=first,
        )
        if len(result.stdout) > 16 * 1024 * 1024:
            raise ValueError("Ответ SSH превышает 16 MiB")
        return result

    def close(self):
        try:
            if self.control.exists():
                self.system.run(["ssh", "-S", str(self.control), "-O", "exit",
                                 "-p", str(self.config["port"]), self.destination], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            self.directory.cleanup()


def save_text(session, name, result):
    (session / f"{name}.txt").write_bytes(result.stdout)
    (session / f"{name}.stderr.txt").write_bytes(result.stderr)
    return result.stdout.decode(errors="replace")


def save_binary(ssh, session, remote, local, kind):
    remote_path(remote)
    result = ssh.read("cat < " + shlex.quote(remote))
    if result.returncode or not result.stdout:
        detail = result.stderr.decode(errors="replace").strip()
        raise ValueError(f"Не скопирован {remote}: {detail or 'пустой ответ'}")
    data = result.stdout
    if kind == "framework":
        if (len(data) < 20 or data[:6] != b"\x7fELF\x01\x01"
                or struct.unpack_from("<HH", data, 16) != (3, 40)):
            raise ValueError(f"{remote}: ожидалась ARM ELF32 shared library")
    else:
        if (len(data) < 20 or data[:6] != b"\x7fELF\x01\x01"
                or struct.unpack_from("<HH", data, 16) not in ((2, 40), (3, 40))):
            raise ValueError(f"{remote}: ожидался ARM ELF32 файл")
    destination = session / local
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(destination)
    return {"kind": kind, "remote_path": remote, "local_path": str(local),
            "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "status": "confirmed", "scope": "read and format validation, not ABI compatibility"}


def route_check(system, session, address, interface, name):
    result = system.run(["route", "-n", "get", address])
    text = save_text(session, name, result)
    match = re.search(r"^\s*interface:\s*(\S+)", text, re.M)
    if result.returncode or not match or match.group(1) != interface:
        raise ValueError(f"Маршрут к {address} не подтверждён через {interface}; см. {name}.txt")


def capture_ssh(config, session, report, system, ssh_factory):
    head = config["head_unit"]["ssh_ip"]
    report["head_unit_ip"] = head
    route_check(system, session, head, report["network"]["interface"], "head-route")
    system.probe(head, config["ssh"]["port"], config["ssh"]["connect_timeout"])
    ssh = ssh_factory(config["ssh"], head, system)
    try:
        print("SSH: при необходимости подтвердите fingerprint и введите пароль в запрос OpenSSH.", flush=True)
        first = ssh.read(TEXT_READS["uname"], first=True)
        report["ssh_first_returncode"] = first.returncode
        platform = save_text(session, "uname", first).strip()
        if first.returncode:
            raise ValueError(f"SSH не выполнен (код {first.returncode}): см. сообщение OpenSSH в Терминале")
        report["platform_raw"] = platform
        words = platform.split()
        if len(words) < 4 or words[0] != "QNX" or words[-1] != "armle":
            raise ValueError("Адрес HU не подтвердился как ARM QNX; дальнейшее чтение остановлено")
        report["os_release"], report["architecture"] = words[2], words[-1]
        for name, command in TEXT_READS.items():
            if name == "uname":
                continue
            result = ssh.read(command)
            text = save_text(session, name, result)
            if name == "nbt-version" and not result.returncode:
                found = FIRMWARE.search(text)
                if found:
                    report["firmware_detected"] = found.group().upper().replace("NBTEVO_", "NBTevo_")
            if name == "display":
                match = re.search(r"resolution:\s*x\s*=\s*(\d+),\s*y\s*=\s*(\d+)", text)
                report["display"] = ({"width": int(match[1]), "height": int(match[2])}
                                     if match and int(match[1]) and int(match[2]) else None)
            if result.returncode:
                report["notes"].append(f"{name}: команда не выполнена, сохранён stderr")
        entered, detected = config["head_unit"]["firmware"], report.get("firmware_detected", "")
        if entered and detected and entered.casefold() != detected.casefold():
            report["issues"].append("Версия из конфига отличается от firmware по SSH")
        report["firmware"] = detected or entered or None
        report["firmware_source"] = "ssh" if detected else ("config" if entered else None)
        report["firmware_status"] = "confirmed" if detected else ("reported" if entered else "unknown")
        if not report["firmware"]:
            report["notes"].append("Добавьте точную версию NBTevo из меню в конфиг/фото сеанса")
        for name, remote in config["capture"]["frameworks"].items():
            try:
                report["files"].append(save_binary(ssh, session, remote, Path("frameworks") / name, "framework"))
            except ValueError as error:
                report["issues"].append(str(error))
                search = ssh.read("find /opt/hmi -name " + shlex.quote(name) + " -print")
                save_text(session, "search-" + name, search)
        directories = " ".join(shlex.quote(x) for x in config["capture"]["runtime_search_dirs"])
        names = " -o ".join("-name " + shlex.quote(x) for x in config["capture"]["runtime_files"])
        search = ssh.read(f"find {directories} \\( {names} \\) -print")
        text = save_text(session, "runtime-paths", search)
        discovered = set(text.splitlines())
        for name, override in config["capture"]["runtime_files"].items():
            candidates = [override] if override else sorted(
                x for x in discovered if PurePosixPath(x).name == name and x.startswith("/")
            )
            if not candidates:
                report["notes"].append(f"Runtime {name}: путь не найден")
            for remote in candidates:
                try:
                    remote_path(remote)
                    relative = Path("runtime").joinpath(*PurePosixPath(remote).parts[1:])
                    report["files"].append(save_binary(ssh, session, remote, relative, "runtime"))
                except ValueError as error:
                    report["notes"].append(str(error))
    finally:
        ssh.close()


def collect(config, root=ROOT, system=None, ssh_factory=SSH):
    system = system or System()
    previous_umask = os.umask(0o077)
    base = Path(config["output_dir"])
    if not base.is_absolute():
        base = root / base
    try:
        base.mkdir(parents=True, exist_ok=True)
        session = Path(tempfile.mkdtemp(prefix=datetime.now(TIMEZONE).strftime("%Y-%m-%d_%H-%M-%S_"), dir=base))
    except OSError:
        os.umask(previous_umask)
        raise
    report = {"date": datetime.now(TIMEZONE).isoformat(), "status": "confirmed",
              "scope": "collection only; vehicle compatibility remains unknown",
              "result": "partial", "files": [], "issues": [], "notes": [],
              "firmware_entered": config["head_unit"]["firmware"] or None}
    try:
        (session / "config-used.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        interfaces = system.run(["ifconfig"])
        text = save_text(session, "ifconfig", interfaces)
        if interfaces.returncode:
            raise ValueError("Не удалось прочитать ifconfig")
        network = choose_network(config["network"], parse_interfaces(text))
        report["network"] = network
        timeout = config["network"]["timeout_seconds"]
        gateway = config["network"]["gateway_ip"]
        if not gateway:
            peers, raw = system.discover(network["local_ip"], network["broadcast"], timeout)
            (session / "discovery.txt").write_text(raw)
            report["gateway_candidates"] = peers
            if len(peers) != 1:
                raise ValueError("Нет единственного шлюза: укажите network.gateway_ip по результату discovery.txt")
            gateway = peers[0]
        report["gateway_ip"] = gateway
        route_check(system, session, gateway, network["interface"], "gateway-route")
        system.probe(gateway, 6801, timeout)
        records = system.identify(gateway, timeout)
        report["identifiers"] = records
        (session / "identify.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
        lines = [f"{r['ecu']} 0x{r['address']:02X} DID 0x{r['did']:04X}: {r['result']}: {r['value']}" for r in records]
        (session / "identify.txt").write_text("\n".join(lines) + "\n")
        if any(r["result"] == "error" for r in records):
            report["issues"].append("Часть диагностических чтений завершилась ошибкой")
        if any(r["result"] == "negative" for r in records):
            report["notes"].append("Есть отрицательные ответы DID; поддержка этих идентификаторов не подтверждена")
        if not config["head_unit"]["ssh_ip"]:
            raise ValueError("ENET сохранён. Укажите отдельный head_unit.ssh_ip для чтения HU")
        capture_ssh(config, session, report, system, ssh_factory)
        report["result"] = "complete" if not report["issues"] else "partial"
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report["issues"].append(str(error))
    except KeyboardInterrupt:
        report["interrupted"] = True
        report["issues"].append("Сеанс прерван пользователем; собранные данные сохранены")
    finally:
        try:
            (session / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            (session / "SHA256SUMS.txt").write_text("".join(
                f"{item['sha256']}  {item['local_path']}\n" for item in report["files"]
            ))
            print(f"Сеанс: {session}\nРезультат: {report['result']}; файлов HU: {len(report['files'])}")
            for item in report["issues"] + report["notes"]:
                print("- " + item)
        finally:
            os.umask(previous_umask)
    return session, report


def dry_plan(config):
    return {
        "network": config["network"],
        "head_unit": config["head_unit"],
        "actions": [
            "Read local ifconfig and routes",
            "If gateway is empty: one UDP 6811 discovery on the selected interface",
            "Read configured candidate identifiers through HSFZ TCP 6801",
            "If head_unit.ssh_ip is set: SSH uname, firmware, screen, CPU",
            "Copy configured HMI libraries; discover and copy named runtime aliases",
            "Write private session logs, summary and SHA256SUMS",
        ],
        "ssh_text_reads": TEXT_READS,
        "frameworks": config["capture"]["frameworks"],
        "writes_to_hu": False,
        "executes_uploaded_programs": False,
        "dry_run": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check", "collect"))
    parser.add_argument("--config", type=Path, default=LOCAL)
    parser.add_argument("--dry-run", action="store_true", help="show collection actions without any connection")
    args = parser.parse_args()
    try:
        if args.command == "init":
            if args.dry_run:
                raise ValueError("--dry-run применяется к collect")
            args.config.parent.mkdir(parents=True, exist_ok=True)
            if args.config.exists():
                print(f"Конфиг уже существует, сохранён без изменений: {args.config}")
            else:
                example = json.loads(EXAMPLE.read_text())
                minimal = {section: {key: example[section][key] for key in fields}
                           for section, fields in (
                               ("network", ("interface",)),
                               ("head_unit", ("ssh_ip", "firmware")),
                               ("ssh", ("legacy",)),
                           )}
                with args.config.open("x") as output:
                    os.chmod(args.config, 0o600)
                    output.write(json.dumps(minimal, ensure_ascii=False, indent=2) + "\n")
                print(f"Создан конфиг: {args.config}")
            return 0
        config = read_config(args.config)
        if args.dry_run:
            if args.command != "collect":
                raise ValueError("--dry-run применяется к collect")
            print(json.dumps(dry_plan(config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "check":
            for command in ("ssh", "ifconfig", "route", "docker"):
                if shutil.which(command) is None:
                    raise ValueError(f"Команда не найдена: {command}")
            for command in ([sys.executable, str(ROOT / "tools/nbt-dev/qnx-deps.py"), "check"],
                            [str(ROOT / "tools/nbt-dev/qnx-run"), "check"]):
                subprocess.run(command, check=True)
            print("Конфиг и локальные инструменты проверены. Соединений с машиной не было.")
            return 0
        _, report = collect(config)
        if report.get("interrupted"):
            return 130
        return 0 if report["result"] == "complete" else 2
    except KeyboardInterrupt:
        print("Операция прервана.", file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
