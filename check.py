#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Installation check for dbus-phoenix-evcharger on a Venus OS GX device.

    sh /data/dbus-phoenix-evcharger/check.sh

Read-only: it changes nothing on the GX, the charger or the meter.
"""

import configparser
import importlib.util
import json
import os
import subprocess
import sys
import time

DIR = os.environ.get("PHX_DIR", os.path.dirname(os.path.realpath(__file__)))
LOG_DIR = os.environ.get("PHX_LOG", "/var/log/dbus-phoenix-evcharger")
SVC_LINK = os.environ.get("PHX_SVC", "/service/dbus-phoenix-evcharger")
RC_LOCAL = os.environ.get("PHX_RC", "/data/rc.local")
NODERED_FLOWS = os.environ.get("PHX_FLOWS", "/data/home/nodered/.node-red/flows.json")
TAI_OFFSET = 4611686018427387914  # multilog tai64n label = seconds + 2^62 + 10

counts = {"OK": 0, "LET OP": 0, "FOUT": 0}


def out(level, text):
    counts[level] += 1
    print("  [%-6s] %s" % (level, text))


def ok(text): out("OK", text)
def warn(text): out("LET OP", text)
def err(text): out("FOUT", text)


def head(text):
    print("\n" + text)


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
head("1. Bestanden en opstarten")
script = os.path.join(DIR, "dbus-phoenix-evcharger.py")
version = "?"
try:
    for line in open(script):
        if line.startswith("VERSION ="):
            version = line.split('"')[1]
            break
    ok("script aanwezig, versie %s" % version)
except OSError:
    err("script niet gevonden in %s" % DIR)

if os.path.islink(SVC_LINK) and os.path.realpath(SVC_LINK) == os.path.realpath(os.path.join(DIR, "service")):
    ok("service gekoppeld in /service")
else:
    err("%s ontbreekt of wijst niet naar %s/service - draai install.sh" % (SVC_LINK, DIR))

st = sh("svstat %s" % SVC_LINK)
if ": up" in st:
    secs = int(st.split(")")[-1].split()[0]) if st.split(")")[-1].split() else 0
    if secs < 20:
        warn("service draait pas %d s - draait hij stabiel? (%s)" % (secs, st.split(": ", 1)[-1]))
    else:
        ok("service draait (%s)" % st.split(": ", 1)[-1])
elif st:
    err("service draait niet: %s" % st)
else:
    warn("svstat gaf geen antwoord (draai je dit op de GX?)")

install_line = os.path.join(DIR, "install.sh")
try:
    rc = open(RC_LOCAL).read()
    if install_line in rc and os.access(RC_LOCAL, os.X_OK):
        ok("rc.local start de service na een reboot")
    elif install_line in rc:
        err("rc.local is niet uitvoerbaar: chmod 755 %s" % RC_LOCAL)
    else:
        err("rc.local bevat geen regel voor %s - draai install.sh" % install_line)
    for line in rc.splitlines():
        if "venus-evcharger" in line and not line.strip().startswith("#"):
            err("rc.local start nog de oude venus-evcharger: %s" % line.strip())
except OSError:
    err("%s ontbreekt - draai install.sh" % RC_LOCAL)
if os.path.exists(RC_LOCAL + ".disabled"):
    err("rc.local.disabled bestaat: zet in de GUI de Modification checks / custom scripts aan")

# --------------------------------------------------------------------------
head("2. config.ini")
cfg = configparser.ConfigParser()
cfg_ok = False
try:
    if cfg.read(os.path.join(DIR, "config.ini")):
        cfg_ok = True
        ok("config.ini is leesbaar")
    else:
        err("config.ini ontbreekt - draai install.sh")
except configparser.Error as e:
    err("config.ini bevat een fout: %s" % str(e).replace("\n", " "))

phoenix_host = meter_host = ""
if cfg_ok:
    d = cfg["DEFAULT"]
    lo, hi, df = d.getint("MinCurrent", 6), d.getint("MaxCurrent", 16), d.getint("DefaultCurrent", 10)
    if 6 <= lo <= df <= hi:
        ok("stroom: min %d A, standaard %d A, max %d A" % (lo, df, hi))
    else:
        err("stroomgrenzen kloppen niet: MinCurrent=%d DefaultCurrent=%d MaxCurrent=%d (min 6, min <= standaard <= max)" % (lo, df, hi))
    if d.getint("Phases", 3) not in (1, 3):
        err("Phases moet 1 of 3 zijn")
    if d.get("EnergyForward", "total").strip().lower() not in ("total", "session"):
        err("EnergyForward moet total of session zijn")
    if cfg.has_section("MODBUS") and cfg["MODBUS"].get("Host", "").strip():
        phoenix_host = cfg["MODBUS"].get("Host").strip()
        ok("Phoenix: %s:%s unit %s" % (phoenix_host, cfg["MODBUS"].get("Port", "502"), cfg["MODBUS"].get("UnitId", "180")))
        if cfg["MODBUS"].getfloat("KeepAliveSeconds", 0) > 0:
            warn("KeepAliveSeconds staat aan: de Phoenix heeft dat niet nodig en het maskeert een tweede Modbus-master")
    else:
        err("[MODBUS] Host ontbreekt")
    if cfg.has_section("METER") and cfg["METER"].get("Host", "").strip():
        meter_host = cfg["METER"].get("Host").strip()
        wo = cfg["METER"].get("WordOrder", "big").strip().lower()
        if wo in ("big", "little"):
            ok("meter: SDM72D-M via %s:%s unit %s, WordOrder=%s" % (meter_host, cfg["METER"].get("Port", "502"), cfg["METER"].get("UnitId", "1"), wo))
        else:
            err("[METER] WordOrder moet big of little zijn")
    else:
        warn("geen meter ingesteld ([METER] Host leeg): vermogen en kWh worden berekend")
    if not cfg.has_section("AUTO"):
        ok("geen [AUTO]-blok: de Auto-modus gebruikt de standaardwaarden")

# --------------------------------------------------------------------------
head("3. D-Bus (wat de GUI en VRM zien)")
bus = None
values = {}
try:
    import dbus
    bus = dbus.SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.SystemBus()
except Exception as e:
    err("geen D-Bus beschikbaar: %s" % e)


def get(service, path):
    try:
        v = bus.get_object(service, path, introspect=False).GetValue(dbus_interface="com.victronenergy.BusItem", timeout=3.0)
    except Exception:
        return None
    if isinstance(v, dbus.Array):
        return None  # Victron's "invalid"
    if isinstance(v, (dbus.String, str)):
        return str(v)
    try:
        f = float(v)
        return int(f) if f == int(f) and not isinstance(v, dbus.Double) else f
    except (TypeError, ValueError):
        return str(v)


STATUS = {0: "Disconnected", 1: "Connected", 2: "Charging", 3: "Charged", 4: "Waiting for sun",
          6: "Waiting for start", 7: "Low SOC", 10: "CP input error"}
service = None
if bus is not None:
    names = sorted(str(n) for n in bus.list_names() if str(n).startswith("com.victronenergy.evcharger"))
    mine = [n for n in names if ".phoenix_" in n]
    others = [n for n in names if n not in mine]
    if len(mine) == 1:
        service = mine[0]
        ok("laadpaal op de D-Bus: %s" % service)
    elif not mine:
        err("geen com.victronenergy.evcharger.phoenix_* op de D-Bus: de service draait niet goed")
    else:
        err("meerdere phoenix-services: %s" % ", ".join(mine))
    for n in others:
        warn("nog een EV-laadpaal op de D-Bus: %s (%s) - de EVCS-tegel toont dan een lijst" % (n, get(n, "/ProductName")))

if service:
    paths = ["/Mgmt/ProcessVersion", "/Connected", "/Status", "/Mode", "/Position", "/StartStop", "/SetCurrent",
             "/MinCurrent", "/MaxCurrent", "/Current", "/Ac/Power", "/Ac/L1/Power", "/Ac/L2/Power", "/Ac/L3/Power",
             "/Session/Energy", "/Session/Time", "/Ac/Energy/Forward", "/AutoStart", "/DeviceInstance",
             "/Meter/Connected", "/Meter/Serial", "/Phoenix/CpState", "/Auto/Reason", "/CustomName"]
    values = {p: get(service, p) for p in paths}
    running = str(values["/Mgmt/ProcessVersion"] or "").split(" ")[0]
    if running == version:
        ok("draaiende versie %s = versie op schijf" % running)
    else:
        err("draaiende versie %s, op schijf staat %s - draai restart.sh" % (running or "?", version))
    required = ["/Status", "/Mode", "/Position", "/StartStop", "/SetCurrent", "/MaxCurrent", "/Current",
                "/Ac/Power", "/Session/Energy", "/Session/Time", "/Ac/Energy/Forward", "/Connected"]
    missing = [p for p in required if values[p] is None]
    if missing:
        err("paden die de GUI nodig heeft ontbreken: %s (update naar de nieuwste versie)" % ", ".join(missing))
    else:
        ok("alle paden die de Victron-GUI voor een EVCS gebruikt zijn aanwezig")
    if values["/Connected"] == 1:
        ok("verbinding met de Phoenix: status %s (CP %s), modus %s" % (
            STATUS.get(values["/Status"], values["/Status"]), values["/Phoenix/CpState"] or "?",
            {0: "Manual", 1: "Auto"}.get(values["/Mode"], values["/Mode"])))
    else:
        err("geen verbinding met de Phoenix (/Connected = %s) - controleer IP, netwerk en unit-ID" % values["/Connected"])
    pos = values["/Position"]
    ok("positie: %s" % {0: "0 = AC-uitgang (essentiele belastingen)", 1: "1 = AC-ingang (netkant)"}.get(pos, pos))
    print("           laden %s, ingesteld %s A (grenzen %s-%s A), nu %s A / %s W, sessie %s kWh, totaal %s kWh" % (
        "aan" if values["/StartStop"] else "uit", values["/SetCurrent"], values["/MinCurrent"], values["/MaxCurrent"],
        values["/Current"], values["/Ac/Power"], values["/Session/Energy"], values["/Ac/Energy/Forward"]))
    if values["/Mode"] == 1:
        print("           Auto: %s" % (values["/Auto/Reason"] or "-"))
    if meter_host:
        if values["/Meter/Connected"] == 1:
            ok("meter verbonden, serienummer %s" % (values["/Meter/Serial"] or "niet uit te lezen"))
        else:
            err("meter is ingesteld maar niet verbonden (/Meter/Connected = %s)" % values["/Meter/Connected"])

# --------------------------------------------------------------------------
head("4. Rechtstreeks uitlezen en vergelijken")
drv = None
try:
    spec = importlib.util.spec_from_file_location("drv", script)
    drv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drv)
except Exception as e:
    err("script kon niet geladen worden: %s" % e)

if drv and cfg_ok and phoenix_host:
    m = cfg["MODBUS"]
    mb = drv.ModbusTcp(phoenix_host, m.getint("Port", 502), m.getint("UnitId", 180), 2.0)
    try:
        raw = mb.read_input(m.getint("StatusInputRegister", 100))[0]
        coil = mb.read_coils(m.getint("EnableCoil", 400))[0]
        amps = mb.read_holding(m.getint("CurrentHoldingRegister", 300))[0]
        letter = chr(raw) if 65 <= raw <= 70 else "?"
        if letter == "?":
            err("Phoenix register 100 geeft %d, geen CP-state A-F: klopt het registeradres?" % raw)
        else:
            ok("Phoenix antwoordt: CP-state %s, vrijgave %s, stroom %d A" % (letter, "aan" if coil else "uit", amps))
        if service and values.get("/Connected") == 1:
            if int(coil) != values["/StartStop"] or amps != values["/SetCurrent"]:
                warn("Phoenix (vrijgave %d, %d A) wijkt af van de D-Bus (StartStop %s, SetCurrent %s): schrijft er nog iets anders naar de Phoenix?" % (
                    coil, amps, values["/StartStop"], values["/SetCurrent"]))
            else:
                ok("Phoenix en D-Bus zijn gelijk")
    except drv.ModbusError as e:
        err("Phoenix niet uit te lezen: %s" % e)
    finally:
        mb.close()

if drv and cfg_ok and meter_host:
    meter = drv.Sdm72Meter(cfg["METER"])
    try:
        r = meter.read()
        ok("meter antwoordt: %.1f/%.1f/%.1f V, %.0f W, %.3f kWh" % (r["v"][0], r["v"][1], r["v"][2], r["p_total"], r["kwh"]))
        low = [v for v in r["v"] if v < 180]
        if low:
            warn("niet alle fasen hebben spanning (%s) - is de meter 3-fasig aangesloten?" % ", ".join("%.0f V" % v for v in r["v"]))
        if service and values.get("/Ac/Energy/Forward") is not None and cfg["DEFAULT"].get("EnergyForward", "total").strip().lower() == "total":
            diff = abs(float(values["/Ac/Energy/Forward"]) - r["kwh"])
            if diff <= 0.05:
                ok("kWh-teller in de GUI is gelijk aan de MID-stand")
            else:
                err("kWh-teller in de GUI (%.3f) wijkt %.3f kWh af van de meter (%.3f)" % (values["/Ac/Energy/Forward"], diff, r["kwh"]))
        if r["p_total"] < -50:
            warn("de meter meet negatief vermogen (%.0f W): staat hij andersom aangesloten?" % r["p_total"])
    except drv.ModbusError as e:
        err("meter niet uit te lezen: %s" % e)
    finally:
        meter.close()

# --------------------------------------------------------------------------
head("5. Log van de afgelopen 24 uur")
since = time.time() - 86400
events = {"charger had": [], "read failed": [], "meter read failed": [], "Traceback": [], "FOUT in config": []}
try:
    files = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("@") or f == "current")
    for name in files:
        for line in open(os.path.join(LOG_DIR, name), errors="replace"):
            if not line.startswith("@") or len(line) < 26:
                continue
            try:
                ts = int(line[1:17], 16) - TAI_OFFSET
            except ValueError:
                continue
            if ts < since:
                continue
            for key in events:
                if key in line and not (key == "read failed" and "meter read failed" in line):
                    events[key].append((ts, line[26:].strip()))
    n = len(events["charger had"])
    if n == 0:
        ok("niemand anders heeft de vrijgave of stroom op de Phoenix gewijzigd")
    else:
        last = time.strftime("%d-%m %H:%M", time.localtime(events["charger had"][-1][0]))
        err("%d keer stond de Phoenix anders dan ingesteld (laatst %s): er schrijft nog een tweede Modbus-master naar de laadpaal" % (n, last))
        print("           laatste: %s" % events["charger had"][-1][1])
    for key, label in (("read failed", "leesfouten Phoenix"), ("meter read failed", "leesfouten meter")):
        n = len(events[key])
        (ok if n == 0 else warn if n < 20 else err)("%s: %d" % (label, n))
    if events["Traceback"] or events["FOUT in config"]:
        err("de service is gecrasht of had een configfout (%d keer) - bekijk de log" % (len(events["Traceback"]) + len(events["FOUT in config"])))
    else:
        ok("geen crashes")
except OSError:
    warn("log niet gevonden in %s" % LOG_DIR)

# --------------------------------------------------------------------------
head("6. Andere regelaars voor deze laadpaal")
for path, what in (("/data/venus-evcharger", "oude venus-evcharger-service"),
                   ("/data/dbus-goecharger", "dbus-goecharger")):
    if os.path.isdir(path):
        warn("%s staat nog in %s" % (what, path))
if os.path.islink("/service/dbus-venus-evcharger") or sh("ps | grep -i venus.evcharger | grep -v grep"):
    err("de oude venus-evcharger-service draait nog en stuurt dezelfde laadpaal aan")
else:
    ok("de oude venus-evcharger-service draait niet")
try:
    flows = json.load(open(NODERED_FLOWS))
    by = {n.get("id"): n for n in flows}
    active = []
    for n in flows:
        if n.get("type") in ("modbus-write", "modbus-flex-write"):
            client = by.get(n.get("server"), {})
            tab = by.get(n.get("z"), {})
            if client.get("tcpHost") == phoenix_host and not n.get("d") and not tab.get("disabled"):
                active.append("%s (tab '%s')" % (n.get("name") or n.get("id"), tab.get("label", "?")))
    if active:
        err("Node-RED schrijft ook naar de Phoenix: %s - zet die flow uit" % ", ".join(active))
    else:
        ok("Node-RED heeft geen actieve Modbus-writes naar de Phoenix")
except (OSError, ValueError):
    ok("geen Node-RED-flows gevonden")
if phoenix_host:
    print("           Let op: Home Assistant, evcc of een ander systeem in het netwerk kan ook rechtstreeks")
    print("           naar %s schrijven. Dat is vanaf de GX niet te zien; punt 5 laat het gevolg zien." % phoenix_host)

# --------------------------------------------------------------------------
print("\nResultaat: %d OK, %d let op, %d fout" % (counts["OK"], counts["LET OP"], counts["FOUT"]))
sys.exit(1 if counts["FOUT"] else 0)
