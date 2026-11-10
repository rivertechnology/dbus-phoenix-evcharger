#!/bin/sh
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SERVICE_NAME=dbus-phoenix-evcharger
RC_LOCAL=/data/rc.local

if [ -L "/service/$SERVICE_NAME" ]; then
    svc -d "/service/$SERVICE_NAME" "/service/$SERVICE_NAME/log"
    rm "/service/$SERVICE_NAME"
fi
pkill -f "$SCRIPT_DIR/dbus-phoenix-evcharger.py"

if [ -f "$RC_LOCAL" ]; then
    grep -vxF "$SCRIPT_DIR/install.sh" "$RC_LOCAL" > "$RC_LOCAL.tmp"
    cat "$RC_LOCAL.tmp" > "$RC_LOCAL"
    rm -f "$RC_LOCAL.tmp"
fi
echo "Uninstalled. Files in $SCRIPT_DIR are kept (incl. state.json)."
