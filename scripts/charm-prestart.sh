#!/bin/sh
# Fail startup if Charm is absent, disabled, invalid, or its patch set conflicts.
set -eu
HERMES=${1:-hermes}
"$HERMES" charm check
"$HERMES" plugins doctor charm --ci
exec "$HERMES" charm apply
