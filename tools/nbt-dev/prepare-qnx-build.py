#!/usr/bin/env python3
"""Cache checksum-pinned GNU sources and adapt the local Docker recipe."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".build/nbt-qnx/sources"
SOURCES = (
    ("gcc/gcc-8.5.0/gcc-8.5.0.tar.xz", "gcc-8.5.0.tar.xz",
     "d308841a511bb830a6100397b0042db24ce11f642dab6ea6ee44842e5325ed50"),
    ("binutils/binutils-2.38.tar.xz", "binutils-2.38.tar.xz",
     "e316477a914f567eccc34d5d29785b8b0f5a10208d36bbacedcc39048ecfe024"),
)
MIRRORS = (
    "https://ftp.gnu.org/gnu/",
    "https://ftp.wayne.edu/gnu/",
    "https://mirror.csclub.uwaterloo.ca/gnu/",
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(source, offline=False):
    remote, name, expected = source
    destination = CACHE / name
    if destination.is_file() and digest(destination) == expected:
        print(f"Verified cached source: {name}", flush=True)
        return
    if offline:
        raise ValueError("Missing verified GNU source; run tools/nbt-env fetch: " + name)
    failures = []
    for mirror in MIRRORS:
        print(f"Downloading {name} from {mirror}", flush=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as output:
                temporary = Path(output.name)
                with urllib.request.urlopen(mirror + remote, timeout=30) as response:
                    received = 0
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        received += len(chunk)
                        if received % (16 * 1024 * 1024) == 0:
                            print(f"{name}: received {received // (1024 * 1024)} MiB", flush=True)
            if digest(temporary) != expected:
                raise ValueError(f"SHA-256 mismatch: {name}")
            temporary.replace(destination)
            print(f"Verified downloaded source: {name}", flush=True)
            return
        except OSError as error:
            failures.append(f"{mirror}: {error}")
            print(f"Download failed for {name}: {error}", flush=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise RuntimeError(f"All GNU source downloads failed for {name}: {failures}")


def main():
    # Only the executable preparation step needs the sibling context helper.
    # nbt_fetch also loads this module by path to read source constants.
    from qnx_inputs import prepare_context

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="linux/arm64", choices=("linux/arm64", "linux/amd64"))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    recipe = (ROOT / "tools/qnx/Dockerfile").read_text()
    base = "FROM --platform=linux/amd64 debian:"
    if recipe.count(base) != 1:
        raise ValueError("Local Dockerfile changed; review native host platform selection")
    recipe = recipe.replace(base, "FROM debian:", 1)
    for kind, name, variable, archive in (
        ("gcc", "gcc-8.5.0.tar.xz", "GCC_VER", "gcc"),
        ("binutils", "binutils-2.38.tar.xz", "BINUTILS_VER", "binutils"),
    ):
        remote = f"gcc/gcc-${{{variable}}}/gcc-${{{variable}}}" if kind == "gcc" else f"binutils/binutils-${{{variable}}}"
        original = f'RUN curl -fsSL "https://ftp.gnu.org/gnu/{remote}.tar.xz" -o /tmp/{archive}.tar.xz && \\\n'
        replacement = (f"COPY --from=qnx-sources {name} /tmp/{archive}.tar.xz\n"
                       f"RUN test -s /tmp/{archive}.tar.xz && \\\n")
        if recipe.count(original) != 1:
            raise ValueError(f"Local Dockerfile changed; review source download step: {kind}")
        recipe = recipe.replace(original, replacement)
    # Preserve configure evidence when the upstream host-tool build fails.
    build_binutils = "bash /opt/binutils-src/build.sh /tmp/binutils.tar.xz /binutils-out"
    diagnostics = (
        "(bash /opt/binutils-src/build.sh /tmp/binutils.tar.xz /binutils-out || { "
        "cat /tmp/binutilsbuild/obj/libiberty/config.log; "
        "cat /tmp/binutilsbuild/obj/libiberty/config.h; "
        "gcc -E -DHAVE_CONFIG_H -I/tmp/binutilsbuild/obj/libiberty "
        "-I/tmp/binutilsbuild/binutils-2.38/include "
        "/tmp/binutilsbuild/binutils-2.38/libiberty/pex-common.c; exit 1; })"
    )
    if recipe.count(build_binutils) != 1:
        raise ValueError("Local Dockerfile changed; review binutils diagnostic step")
    recipe = recipe.replace(build_binutils, diagnostics)
    recipe = recipe.replace("FROM qnx-sdp AS binutils-build\n", "FROM qnx-sdp AS binutils-deps\n", 1)
    recipe = recipe.replace("RUN test -s /tmp/binutils.tar.xz", "FROM binutils-deps AS binutils-build\nRUN test -s /tmp/binutils.tar.xz", 1)
    if args.platform == "linux/arm64":
        source_start = recipe.index("RUN printf 'deb [check-valid-until=no]")
        source_end = recipe.index(" > /etc/apt/sources.list", source_start) + len(" > /etc/apt/sources.list")
        sources = "".join(
            f"deb [check-valid-until=no] file:/qnx-apt-mirror/{folder} {suite} main\\n"
            for folder, suite in (("debian", "bullseye"), ("security", "bullseye-security"), ("debian", "bullseye-updates"))
        )
        recipe = recipe[:source_start] + f"RUN printf '{sources}' > /etc/apt/sources.list" + recipe[source_end:]
        mount = ("--mount=type=bind,from=qnx-debs,target=/qnx-debs,ro "
                 "--mount=type=bind,from=qnx-apt-mirror,target=/qnx-apt-mirror,ro ")
        recipe = recipe.replace("RUN dpkg --add-architecture", "RUN " + mount + "dpkg --add-architecture")
        recipe = recipe.replace("RUN apt-get update && apt-get install", "RUN " + mount + "apt-get update && apt-get install")
        recipe = recipe.replace(
            "apt-get install -y --no-install-recommends",
            "mkdir -p /var/cache/apt/archives && cp /qnx-debs/*.deb /var/cache/apt/archives/ && apt-get install --no-download -y --no-install-recommends",
        )
    (ROOT / f".build/nbt-qnx/debs-{args.platform.rsplit('/', 1)[1]}").mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda source: fetch(source, args.offline), SOURCES))
    generated = ROOT / ".build/nbt-qnx/generated/Dockerfile"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("# Generated by tools/nbt-dev/prepare-qnx-build.py\n" + recipe)
    print(f"Prepared {generated}", flush=True)
    context = prepare_context(ROOT / "tools/qnx", generated.parent / "context")
    print(f"Prepared versioned QNX context: {context}", flush=True)


if __name__ == "__main__":
    main()
