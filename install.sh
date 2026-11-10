#!/bin/sh
# Registers the service with runit and makes sure it comes back after every
# reboot / firmware update (via /data/rc.local). Safe to run repeatedly.

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SERVICE_NAME=dbus-phoenix-evcharger
RC_LOCAL=/data/rc.local
LINE="$SCRIPT_DIR/install.sh"

chmod 755 "$SCRIPT_DIR/dbus-phoenix-evcharger.py" "$SCRIPT_DIR/service/run" \
          "$SCRIPT_DIR/service/log/run" "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/uninstall.sh" "$SCRIPT_DIR/restart.sh"

if [ ! -L "/service/$SERVICE_NAME" ]; then
    ln -s "$SCRIPT_DIR/service" "/service/$SERVICE_NAME"
fi

if [ ! -f "$RC_LOCAL" ]; then
    printf '#!/bin/sh\n\n' > "$RC_LOCAL"
    chmod 755 "$RC_LOCAL"
fi
grep -qxF "$LINE" "$RC_LOCAL" || echo "$LINE" >> "$RC_LOCAL"

if [ -f /data/rc.local.disabled ]; then
    echo "LET OP: /data/rc.local.disabled bestaat. Zet in de GUI onder"
    echo "Settings > General > Modification checks de custom scripts aan,"
    echo "anders draait rc.local niet na een reboot."
fi
