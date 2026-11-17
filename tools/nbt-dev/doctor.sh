#!/bin/sh
set -eu
printf 'Linux architecture: '
uname -m
python3 - <<'PY'
import io
import PIL
from PIL import Image
from elftools.elf.elffile import ELFFile
import capstone
import elftools
print('Python analysis modules OK')
print('Pillow', PIL.__version__)
print('Capstone', capstone.__version__)
print('pyelftools', elftools.__version__)
with open('/bin/true', 'rb') as stream:
    assert ELFFile(stream)['e_machine'] == 'EM_X86_64'
assert list(capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM).disasm(bytes.fromhex('0000a0e3'), 0))[0].mnemonic == 'mov'
output = io.BytesIO()
Image.new('RGBA', (8, 8)).save(output, format='PNG')
assert output.getvalue().startswith(b'\x89PNG')
print('ELF parsing, ARM disassembly and PNG generation OK')
PY
cc --version | head -n 1
readelf --version | head -n 1
if [ -x "$QNX_HOST/usr/bin/qcc" ]; then
    printf '\nQNX compiler selections\n'
    qcc -V
    printf '\nQNX compiler host binary\n'
    file "$QNX_HOST/usr/bin/qcc"
else
    printf '\nQNX SDK is not mounted. Set NBT_QNX_SDK to a QNX SDP 6.5 directory.\n'
fi
if [ -f /frameworks/libAppFramework.A.so ] && [ -f /frameworks/libCoreFramework.A.so ]; then
    printf '\nHMI framework hashes\n'
    sha256sum /frameworks/libAppFramework.A.so /frameworks/libCoreFramework.A.so
else
    printf '\nHMI frameworks are not mounted. Set NBT_FRAMEWORKS after capture from HU.\n'
fi
