#!/bin/sh
# Read-only installation check. Usage: sh /data/dbus-phoenix-evcharger/check.sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 "$SCRIPT_DIR/check.py"
