#!/bin/sh
# Runs only in a temporary dependency-download container, never on macOS.
set -eu
mode="$1"
trap 'chmod -R a+rX /cache' EXIT
export DEBIAN_FRONTEND=noninteractive
rm -f /etc/apt/apt.conf.d/docker-clean
if [ "${NBT_APT_I386:-0}" = 1 ]; then dpkg --add-architecture i386; fi
if [ "$mode" = apt ]; then
    apt-get update
    xargs -r apt-get install -y --download-only --no-install-recommends < /recipes/apt-packages.txt
    exit 0
fi
# All host OS packages are already in the mounted cache. No apt download here.
xargs -r apt-get install -y --no-download --no-install-recommends < /recipes/apt-packages.txt
case "$mode" in
    wheels)
        python3 -m pip download --only-binary=:all: --progress-bar on \
            --dest /cache/wheels -r /recipes/python-requirements.txt ;;
    npm)
        tar -xJf /artifacts/node-v24.22.3-linux-arm64.tar.xz -C /usr/local --strip-components=1
        mkdir -p /tmp/rea-prefetch
        cd /tmp/rea-prefetch
        npm install --omit=dev --ignore-scripts --no-audit --no-fund \
            --cache /cache/npm /artifacts/rea-agents-6.9.0.tgz
        npm cache verify --cache /cache/npm ;;
    *) printf 'Unknown prefetch mode: %s\n' "$mode" >&2; exit 2 ;;
esac
