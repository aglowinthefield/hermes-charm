#!/bin/sh
# Fail startup if Charm is absent, disabled, invalid, or its patch set conflicts.
set -eu
HERMES=${1:-hermes}
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$HERMES" charm check
"$HERMES" plugins doctor charm --ci
exec sh "$HERE/local-patches-ensure.sh"
