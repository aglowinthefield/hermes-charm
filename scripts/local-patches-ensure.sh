#!/bin/sh
# Explicit companion installer. Native plugin loading never applies patches.
set -eu
HERMES=hermes
if [ "${1:-}" = --hermes ]; then
    HERMES=$2
    shift 2
fi
exec "$HERMES" charm apply "$@"
