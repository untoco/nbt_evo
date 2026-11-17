#!/usr/bin/env python3
"""Cache signed Debian indexes; apt verifies their signatures during the build."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".build/nbt-qnx/metadata/apt-mirror"
SNAPSHOT = "20260831T235959Z"
SUITES = (
    ("debian", "debian", "bullseye"),
    ("security", "debian-security", "bullseye-security"),
    ("debian", "debian", "bullseye-updates"),
)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url, destination, expected=None, offline=False):
    if destination.is_file():
        if expected is None or (destination.stat().st_size == expected[1]
                                and sha256(destination) == expected[0]):
            destination.chmod(0o644)
            return
    if offline:
        raise ValueError("Missing verified Debian index; run tools/nbt-env fetch: " + str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        print(f"Downloading Debian metadata: {destination.relative_to(CACHE)}", flush=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(url, timeout=30) as response:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
        if expected and (temporary.stat().st_size != expected[1] or sha256(temporary) != expected[0]):
            raise ValueError(f"Debian metadata checksum mismatch: {destination}")
        temporary.replace(destination)
        destination.chmod(0o644)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_suite(suite, offline=False):
    local, archive, name = suite
    remote = f"https://snapshot.debian.org/archive/{archive}/{SNAPSHOT}/dists/{name}/"
    target = CACHE / local / "dists" / name
    release = target / "InRelease"
    fetch(remote + "InRelease", release, offline=offline)
    text = release.read_text()
    if not text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        raise ValueError(f"Expected signed InRelease: {release}")
    checksums = {}
    inside = False
    for line in text.splitlines():
        if line == "SHA256:":
            inside = True
        elif inside and line.startswith(" "):
            digest, size, path = line.split()
            checksums[path] = (digest, int(size))
        elif inside:
            break
    for arch in ("arm64", "i386"):
        path = f"main/binary-{arch}/Packages.xz"
        expected = checksums[path]
        destination = target / path
        fetch(remote + path, destination, expected, offline=offline)
        by_hash = destination.parent / "by-hash/SHA256" / expected[0]
        by_hash.parent.mkdir(parents=True, exist_ok=True)
        if not by_hash.is_symlink():
            by_hash.symlink_to("../../Packages.xz")
    print(f"Cached signed metadata: {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda suite: prepare_suite(suite, args.offline), SUITES))
    print("Debian index files verified against InRelease hashes; apt signature check still required.")
