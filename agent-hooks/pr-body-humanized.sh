#!/bin/sh
# Compatibility shim: Hermes sessions started before the move to
# pr-body-humanized.py still call this path until they restart. Delete it once
# no running gateway/worker has the old path loaded.
exec python3 "$(dirname "$0")/pr-body-humanized.py"
