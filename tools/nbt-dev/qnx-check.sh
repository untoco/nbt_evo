#!/bin/sh
set -eu
compiler=arm-unknown-nto-qnx6.5.0eabi-gcc
out=/work/.build/nbt-qnx/out
mkdir -p "$out"
"$compiler" --version | head -n 1
"$compiler" -dumpmachine
"$compiler" -std=c99 -Wall -Wextra -Werror -O2 \
    /work/tools/nbt-dev/examples/hello-qnx.c -o "$out/hello-qnx"
file "$out/hello-qnx"
arm-unknown-nto-qnx6.5.0eabi-readelf -h -l -d -A "$out/hello-qnx" > "$out/hello-qnx.elf.txt"
grep -E 'Class:|Machine:|Flags:|interpreter:|NEEDED|Tag_CPU_arch:|Tag_ABI_VFP_args:' "$out/hello-qnx.elf.txt"
grep -q 'Class:.*ELF32' "$out/hello-qnx.elf.txt"
grep -q 'Machine:.*ARM' "$out/hello-qnx.elf.txt"
grep -q 'interpreter: /usr/lib/ldqnx.so.2' "$out/hello-qnx.elf.txt"
grep -q 'Shared library: \[libc.so.3\]' "$out/hello-qnx.elf.txt"
sha256sum "$out/hello-qnx" > "$out/hello-qnx.sha256"
arm-unknown-nto-qnx6.5.0eabi-g++ -std=gnu++98 -Wall -Wextra -Werror -O2 \
    -fno-exceptions -fno-rtti -static-libstdc++ -static-libgcc \
    /work/tools/nbt-dev/examples/hello-qnx.cpp -o "$out/hello-qnx-cxx"
file "$out/hello-qnx-cxx"
arm-unknown-nto-qnx6.5.0eabi-readelf -h -l -d -A "$out/hello-qnx-cxx" > "$out/hello-qnx-cxx.elf.txt"
grep -q 'Machine:.*ARM' "$out/hello-qnx-cxx.elf.txt"
grep -q 'interpreter: /usr/lib/ldqnx.so.2' "$out/hello-qnx-cxx.elf.txt"
if grep -q 'Shared library: \[libstdc++' "$out/hello-qnx-cxx.elf.txt"; then
    printf 'Unexpected shared C++ runtime dependency\n' >&2
    exit 1
fi
grep -E 'interpreter:|NEEDED' "$out/hello-qnx-cxx.elf.txt"
sha256sum "$out/hello-qnx-cxx" > "$out/hello-qnx-cxx.sha256"
printf '\nBuilt /work/.build/nbt-qnx/out/hello-qnx. Execution on QNX has not been tested.\n'
