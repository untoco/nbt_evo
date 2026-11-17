#!/usr/bin/env python3
"""Download or verify local Debian build inputs; never install them on macOS."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = ROOT / "tools/qnx-deps"
CACHE = ROOT / ".build/nbt-qnx/debs-arm64"
MANIFEST = Path(__file__).with_name("dependencies-arm64.json")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def valid(path, package):
    return (path.is_file() and path.stat().st_size == package["size"]
            and sha256(path) == package["sha256"])


def normalize(packages):
    """Accept browser filenames and copy verified files to apt's cache names."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    known = {sha256(path): path for path in DOWNLOADS.rglob("*.deb") if path.is_file()}
    missing = []
    for package in packages:
        target = CACHE / package["filename"]
        if valid(target, package):
            continue
        source = known.get(package["sha256"])
        if source is None or source.stat().st_size != package["size"]:
            missing.append(package)
            continue
        shutil.copyfile(source, target)
        if not valid(target, package):
            target.unlink(missing_ok=True)
            raise ValueError(f"Copied package failed verification: {target.name}")
    return missing


def download(package):
    destination = DOWNLOADS / package["filename"]
    temporary = None
    try:
        print(f"Downloading {package['filename']}", flush=True)
        with tempfile.NamedTemporaryFile(dir=DOWNLOADS, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(package["url"], timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        if not valid(temporary, package):
            raise ValueError(f"Size/SHA-256 mismatch: {package['filename']}")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "check"))
    args = parser.parse_args()
    packages = json.loads(MANIFEST.read_text())["packages"]
    missing = normalize(packages)
    if missing and args.command == "download":
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(download, missing))
        missing = normalize(packages)
    if missing:
        print(f"Missing {len(missing)} of {len(packages)} Debian packages.")
        print(f"Put .deb files in {DOWNLOADS}")
        print(f"Download links: {MANIFEST.with_suffix('.urls.txt')}")
        raise SystemExit(1)
    print(f"Verified {len(packages)} Debian packages; ready in {CACHE}")


if __name__ == "__main__":
    main()
