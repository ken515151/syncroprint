#!/bin/bash
# Build the .deb in a clean Ubuntu container; the result lands in dist/.
# Works from Linux or from Git Bash on Windows (Docker Desktop).
# Usage: bash packaging/build-deb.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$REPO_DIR/dist"

# Git Bash mangles POSIX-looking paths in arguments to native executables;
# hand Docker Windows-style paths and disable conversion for this call.
SRC_MOUNT="$REPO_DIR"
OUT_MOUNT="$REPO_DIR/dist"
if command -v cygpath >/dev/null 2>&1; then
    SRC_MOUNT="$(cygpath -w "$REPO_DIR")"
    OUT_MOUNT="$(cygpath -w "$REPO_DIR/dist")"
    export MSYS_NO_PATHCONV=1
fi

docker run --rm \
    -v "$SRC_MOUNT:/src:ro" \
    -v "$OUT_MOUNT:/out" \
    ubuntu:24.04 \
    bash -c '
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq debhelper devscripts lintian >/dev/null

        # Build from a copy: keeps the mount read-only, and fixes CRLF line
        # endings / lost exec bits a Windows checkout may have.
        cp -r /src /build
        cd /build
        rm -rf dist __pycache__ */__pycache__
        find debian packaging -type f -print0 | xargs -0 sed -i "s/\r$//"
        # A Windows mount marks every file executable; debhelper then tries
        # to *run* debian/* config files. Strip, then re-add where needed.
        find . -type f -exec chmod -x {} +
        chmod +x debian/rules packaging/*.sh

        dpkg-buildpackage -us -uc -b
        lintian --fail-on error ../syncroprint_*.deb || exit 1
        cp -v /syncroprint_*.deb /out/
    '

echo
echo "Built:"
ls -l "$REPO_DIR"/dist/*.deb
