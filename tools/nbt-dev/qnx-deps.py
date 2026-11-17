#!/usr/bin/env python3
"""Download or verify local Debian build inputs; never install them on macOS."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import stat
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
DOWNLOADS = ROOT / "tools/qnx-deps"
CACHE = ROOT / ".build/nbt-qnx/debs-arm64"
MANIFEST = Path(__file__).with_name("dependencies-arm64.json")


def sha256(path):
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise OSError(error.errno, f"Cannot hash Debian package: {error.strerror}", str(path)) from error


def resident(path):
    """Do not trigger restoration of macOS dataless placeholders."""
    return path.is_file() and not (getattr(path.stat(), "st_flags", 0)
                                  & getattr(stat, "SF_DATALESS", 0x40000000))


def valid(path, package):
    return (resident(path) and path.stat().st_size == package["size"]
            and sha256(path) == package["sha256"])


def normalize(packages):
    """Prefer verified cache/exact names; inspect resident aliases only if needed."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    missing = []
    for package in packages:
        target = CACHE / package["filename"]
        if valid(target, package):
            continue
        source = DOWNLOADS / package["filename"]
        if valid(source, package):
            shutil.copyfile(source, target)
            if not valid(target, package):
                raise ValueError(f"Copied package failed verification: {target.name}")
        else:
            missing.append(package)
    if not missing:
        return []
    # Browser-renamed files remain supported, without reading offloaded duplicates
    # or unrelated packages when all required inputs were already found.
    required_sizes = {item["size"] for item in missing}
    known = {}
    for path in DOWNLOADS.rglob("*.deb"):
        if not resident(path):
            print(f"Skipping nonresident Debian candidate: {path.name}", flush=True)
            continue
        if path.stat().st_size in required_sizes:
            known[sha256(path)] = path
    unresolved = []
    for package in missing:
        target = CACHE / package["filename"]
        source = known.get(package["sha256"])
        if source is None or source.stat().st_size != package["size"]:
            unresolved.append(package)
            continue
        shutil.copyfile(source, target)
        if not valid(target, package):
            raise ValueError(f"Copied package failed verification: {target.name}")
    return unresolved


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
