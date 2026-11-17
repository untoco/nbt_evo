#!/usr/bin/env python3
"""Pinned local Ghidra/REA environment. All analysis containers use --network none."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from nbt_download import download, DEFAULTS as DOWNLOAD_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "tools/nbt-re/artifacts.lock.json"
EXAMPLE = ROOT / "config/nbt/re.example.json"
LOCAL = ROOT / "config/nbt/re.local.json"


def digest(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def config(path):
    cfg = json.loads(path.read_text())
    if cfg["platform"] != "linux/arm64":
        raise ValueError("This prepared environment supports linux/arm64 only")
    for key in ("timeout_seconds", "startup_timeout_seconds", "decompile_timeout_seconds", "cpus"):
        if not isinstance(cfg[key], int) or not 1 <= cfg[key] <= 7200:
            raise ValueError("Invalid configuration: " + key)
    if not re.fullmatch(r"[0-9]+[MG]", cfg["java_heap"]):
        raise ValueError("Invalid java_heap")
    for key, default in DOWNLOAD_DEFAULTS.items():
        value = cfg.setdefault(key, default)
        maximum = 20 if key == "download_attempts" else (600 if key == "download_stall_seconds" else 86400)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("Invalid configuration: " + key)
    data = Path(cfg["data_dir"])
    if not data.is_absolute():
        data = ROOT / data
    data = data.resolve()
    if not data.is_relative_to(ROOT / ".build"):
        raise ValueError("data_dir must be inside this repository's ignored .build directory")
    data.mkdir(parents=True, exist_ok=True)
    for name in ("artifacts", "reports", "projects", "home", "inputs"):
        (data / name).mkdir(exist_ok=True)
    cfg["data"] = data
    return cfg


def verify(cfg):
    for item in json.loads(LOCK.read_text()).values():
        path = cfg["data"] / "artifacts" / item["file"]
        algorithm = "sha512" if "sha512" in item else "sha256"
        if not path.is_file() or digest(path, algorithm) != item[algorithm]:
            raise ValueError("Missing or incorrect artifact: " + str(path))


def prepare(cfg):
    for item in json.loads(LOCK.read_text()).values():
        path = cfg["data"] / "artifacts" / item["file"]
        algorithm = "sha512" if "sha512" in item else "sha256"
        if path.exists() and digest(path, algorithm) == item[algorithm]:
            print("Verified:", path.name, flush=True)
            continue
        download(item["url"], path, item[algorithm], algorithm=algorithm,
                 settings=cfg, progress=cfg.get("download_progress", True))


def run_container(cfg, command, target=None, label="report", stream=False):
    name = "nbt-evo-re-" + uuid.uuid4().hex[:12]
    options = ["docker", "run", "--rm", "--pull", "never", "--name", name,
               "--platform", cfg["platform"], "--network", "none",
               "--cpus", str(cfg["cpus"]), "--memory", cfg["memory"],
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--mount", f"type=bind,src={cfg['data']},dst=/state",
               "--env", "GHIDRA_HEADLESS_MAXMEM=" + cfg["java_heap"],
               "--env", "REA_GHIDRA_STARTUP_TIMEOUT_MS=" + str(cfg["startup_timeout_seconds"] * 1000),
               "--env", "REA_GHIDRA_DECOMPILE_TIMEOUT_SECONDS=" + str(cfg["decompile_timeout_seconds"])]
    sha = None
    if target:
        if "," in str(target):
            raise ValueError("Input paths containing commas are not supported by Docker --mount")
        sha = digest(target)
        # Mount only this file, not its directory or the user's whole repository.
        options += ["--mount", f"type=bind,src={target},dst=/input/binary,readonly",
                    "--env", "REA_GHIDRA_CHECKPOINT_DIR=/state/projects/" + sha]
    if stream:
        options += ["--mount", f"type=bind,src={cfg['data'] / 'inputs'},dst=/state/inputs,readonly"]
        options.append("-i")
    cmd = options + [cfg["image"]] + command
    if stream:
        try:
            return subprocess.call(cmd)
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = cfg["data"] / "reports" / (stamp + "-" + label + "-" + uuid.uuid4().hex[:6])
    out.mkdir(mode=0o700)
    started = time.monotonic()
    timed_out = False
    interrupted = False
    with (out / "result.json").open("wb") as stdout, (out / "stderr.log").open("wb") as stderr:
        try:
            result = subprocess.run(cmd, stdout=stdout, stderr=stderr, timeout=cfg["timeout_seconds"])
            code = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            code = 124
        except KeyboardInterrupt:
            interrupted = True
            code = 130
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    meta = {"status": "measured", "utc": stamp, "image": cfg["image"],
            "command": command, "input": str(target) if target else None, "sha256": sha,
            "exit_code": code, "timeout": timed_out, "interrupted": interrupted,
            "seconds": round(time.monotonic()-started, 3),
            "network": "none", "input_read_only": bool(target)}
    info = subprocess.run(["docker", "image", "inspect", cfg["image"], "--format", "{{.Id}}"],
                          capture_output=True, text=True)
    meta["image_id"] = info.stdout.strip() if info.returncode == 0 else None
    if target:
        meta["input_unchanged"] = digest(target) == sha
        if not meta["input_unchanged"]:
            code = meta["exit_code"] = 2
    (out / "session.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({"exit_code": code, "report_dir": str(out)}, indent=2))
    if code:
        print((out / "stderr.log").read_text(errors="replace")[-3000:], file=sys.stderr)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=LOCAL if LOCAL.exists() else EXAMPLE)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "prepare", "build", "doctor", "mcp", "mcp-config"):
        sub.add_parser(name)
    for name in ("analyze", "decompile", "search"):
        p = sub.add_parser(name)
        p.add_argument("input", type=Path)
        if name == "decompile":
            p.add_argument("procedure")
        if name == "search":
            p.add_argument("query")
    args = parser.parse_args()
    if args.command == "init":
        if not LOCAL.exists():
            with LOCAL.open("x") as f:
                os.chmod(LOCAL, 0o600)
                f.write(EXAMPLE.read_text())
        print(LOCAL)
        return 0
    cfg = config(args.config)
    if args.command == "mcp-config":
        result = {"mcpServers": {"nbt-evo-re": {
            "command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--config", str(args.config.resolve()), "mcp"]}}}
        destination = cfg["data"] / "mcp.json"
        destination.write_text(json.dumps(result, indent=2) + "\n")
        print(destination)
        return 0
    if args.command == "prepare":
        prepare(cfg)
        return 0
    if args.command == "build":
        verify(cfg)
        cmd = ["docker", "build", "--network", "none", "--platform", cfg["platform"],
               "--build-context", "re-apt=" + str(ROOT / ".build/downloads/re/apt"),
               "--build-context", "re-npm=" + str(ROOT / ".build/downloads/re/npm"),
               "--build-context", "recipes=" + str(ROOT / "tools/nbt-re"),
               "-f", str(ROOT / "tools/nbt-re/Dockerfile")]
        if "base_image" in cfg:
            cmd += ["--build-arg", "BASE=" + cfg["base_image"]]
        return subprocess.call(cmd + ["-t", cfg["image"], str(cfg["data"] / "artifacts")])
    if args.command == "doctor":
        return run_container(cfg, ["rea", "doctor", "--provider", "ghidra", "--json"], label="doctor")
    if args.command == "mcp":
        return run_container(cfg, ["rea", "mcp"], stream=True)
    target = args.input.expanduser().resolve(strict=True)
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError("Expected a nonempty regular binary file")
    command = ["rea", args.command, "/input/binary"]
    if args.command == "decompile":
        command += ["--procedure", args.procedure]
    if args.command == "search":
        command += [args.query, "--kind", "procedures"]
    command += ["--provider", "ghidra", "--json"]
    return run_container(cfg, command, target, args.command)


if __name__ == "__main__":
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
