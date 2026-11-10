#!/bin/sh
# Update from GitHub. Keeps config.ini and state.json, makes a full backup first.
#   sh /data/dbus-phoenix-evcharger/update.sh          -> branch main
#   sh /data/dbus-phoenix-evcharger/update.sh <branch>
# Nothing runs automatically: only when you start this script.

REPO=rivertechnology/dbus-phoenix-evcharger
BRANCH=${1:-main}
DIR=/data/dbus-phoenix-evcharger
BAK=/data/dbus-phoenix-evcharger.bak
SVC=/service/dbus-phoenix-evcharger
TMP=/tmp/dbus-phoenix-evcharger-update
URL=https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz

# Run from a copy in /tmp, so overwriting this file during the update is safe
if [ "$0" != "/tmp/dbus-phoenix-evcharger-update.sh" ]; then
    cp "$0" /tmp/dbus-phoenix-evcharger-update.sh
    exec sh /tmp/dbus-phoenix-evcharger-update.sh "$@"
fi

fail() { echo "FOUT: $*"; echo "Er is niets gewijzigd aan $DIR."; rm -rf "$TMP"; exit 1; }

rm -rf "$TMP"; mkdir -p "$TMP"
echo "Downloaden $URL"
if command -v wget >/dev/null 2>&1; then
    wget -q -O "$TMP/src.tar.gz" "$URL" || fail "download mislukt"
else
    curl -fsSL -o "$TMP/src.tar.gz" "$URL" || fail "download mislukt"
fi
tar xzf "$TMP/src.tar.gz" -C "$TMP" || fail "uitpakken mislukt"
SRC="$TMP/dbus-phoenix-evcharger-$BRANCH"
[ -f "$SRC/dbus-phoenix-evcharger.py" ] || fail "onverwachte inhoud van het archief"
python3 -I -m py_compile "$SRC/dbus-phoenix-evcharger.py" || fail "nieuwe versie bevat een fout"
rm -rf "$SRC/__pycache__" "$SRC/config.ini" "$SRC/state.json"

OLD=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DIR/dbus-phoenix-evcharger.py" 2>/dev/null)
NEW=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/dbus-phoenix-evcharger.py")
echo "Huidige versie: ${OLD:-?}  nieuwe versie: $NEW"

# Stop service (it saves state.json on SIGTERM), then back up the complete folder
[ -L "$SVC" ] && svc -d "$SVC" && sleep 3
rm -rf "$BAK"
cp -a "$DIR" "$BAK" || fail "backup mislukt"
echo "Backup: $BAK"

cp -a "$SRC/." "$DIR/" || { echo "FOUT bij kopieren, backup terugzetten..."; cp -a "$BAK/." "$DIR/"; svc -u "$SVC"; exit 1; }

sh "$DIR/install.sh"
svc -u "$SVC"
rm -rf "$TMP"

# Report settings that are new in config.default.ini (code uses defaults for them)
python3 -I - "$DIR/config.default.ini" "$DIR/config.ini" <<'EOF'
import configparser, sys
def load(p):
    c = configparser.ConfigParser(); c.optionxform = str; c.read(p); return c
d, c = load(sys.argv[1]), load(sys.argv[2])
missing = [k for k in d.defaults() if k not in c.defaults()]
missing = ["[DEFAULT] %s=%s" % (k, d.defaults()[k]) for k in missing]
for s in d.sections():
    for k, v in d._sections[s].items():
        if not (c.has_section(s) and k in c._sections[s]):
            missing.append("[%s] %s=%s" % (s, k, v))
if missing:
    print("Nieuwe instellingen (gebruiken nu hun standaardwaarde, zie config.default.ini):")
    for m in missing: print("  " + m)
EOF

sleep 3
svstat "$SVC"
echo "Klaar. Terugdraaien: svc -d $SVC; cp -a $BAK/. $DIR/; svc -u $SVC"
