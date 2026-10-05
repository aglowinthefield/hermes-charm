#!/bin/sh
# Explicit companion installer. Native plugin loading never applies patches.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$HERE/../local_patches.py" "$@"
