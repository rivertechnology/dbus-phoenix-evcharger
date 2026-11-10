#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbus-phoenix-evcharger

Minimal Venus OS driver for a Phoenix Contact EM-CP-PP-ETH charge controller
over Modbus TCP, published as com.victronenergy.evcharger.

- Manual mode only (start/stop + charge current from the GX GUI)
- No energy meter: power and energy are CALCULATED as
  setpoint current x voltage x phases while the car is in state C/D
- Total energy counter persisted in /data so it survives restarts
- No external Python dependencies (own minimal Modbus TCP client)
"""

import configparser
import json
import logging
import os
import socket
import struct
import sys
import time

VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

VELIB_PATHS = [
    os.path.join(BASE_DIR, "ext", "velib_python"),
    "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
]

# Victron evcharger /Status codes
ST_DISCONNECTED = 0
ST_CONNECTED = 1
ST_CHARGING = 2
ST_CHARGED = 3
ST_WAIT_START = 6
ST_CP_SHORTED = 10


# --------------------------------------------------------------------------
# Minimal Modbus TCP client
# --------------------------------------------------------------------------
class ModbusError(Exception):
    pass


class ModbusTcp:
    def __init__(self, host, port=502, unit=1, timeout=2.0):
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self._sock = None
        self._tid = 0

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self):
        if self._sock is None:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
            s.settimeout(self.timeout)
            self._sock = s

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("connection closed by peer")
            buf += chunk
        return buf

    def _request(self, fc, payload):
        try:
            self._connect()
            self._tid = (self._tid + 1) & 0xFFFF
            pdu = bytes([fc]) + payload
            mbap = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit)
            self._sock.sendall(mbap + pdu)
            hdr = self._recv_exact(7)
            tid, _pid, length, _unit = struct.unpack(">HHHB", hdr)
            body = self._recv_exact(length - 1)
        except (OSError, ModbusError) as e:
            self.close()
            raise ModbusError("%s:%s %s" % (self.host, self.port, e))
        if tid != self._tid:
            self.close()
            raise ModbusError("transaction id mismatch")
        rfc = body[0]
        if rfc == (fc | 0x80):
            raise ModbusError("exception code %d (fc %d)" % (body[1], fc))
        if rfc != fc:
            self.close()
            raise ModbusError("unexpected function code %d" % rfc)
        return body[1:]

    def read_coils(self, addr, count=1):
        data = self._request(0x01, struct.pack(">HH", addr, count))
        bits = data[1:]
        return [bool(bits[i // 8] >> (i % 8) & 1) for i in range(count)]

    def _read_regs(self, fc, addr, count):
        data = self._request(fc, struct.pack(">HH", addr, count))
        return list(struct.unpack(">%dH" % count, data[1:1 + 2 * count]))

    def read_holding(self, addr, count=1):
        return self._read_regs(0x03, addr, count)

    def read_input(self, addr, count=1):
        return self._read_regs(0x04, addr, count)

    def write_coil(self, addr, value):
        self._request(0x05, struct.pack(">HH", addr, 0xFF00 if value else 0x0000))

    def write_register(self, addr, value):
        self._request(0x06, struct.pack(">HH", addr, int(value) & 0xFFFF))


# --------------------------------------------------------------------------
# Persistent state (energy counter)
# --------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        logging.warning("could not save state: %s", e)


# --------------------------------------------------------------------------
# Charger service
# --------------------------------------------------------------------------
class PhoenixEvCharger:
    def __init__(self, cfg, dbus_service_cls):
        d = cfg["DEFAULT"]
        self.poll_ms = d.getint("PollIntervalMs", 2000)
        self.voltage = d.getfloat("Voltage", 230.0)
        self.phases = d.getint("Phases", 3)
        self.current_factor = d.getfloat("CurrentFactor", 1.0)
        self.min_current = d.getint("MinCurrent", 6)
        self.max_current = d.getint("MaxCurrent", 16)
        self.energy_mode = d.get("EnergyForward", "total").strip().lower()
        self.fail_limit = d.getint("FailuresBeforeDisconnect", 3)

        m = cfg["MODBUS"]
        self.reg_status = m.getint("StatusInputRegister", 100)
        self.reg_current = m.getint("CurrentHoldingRegister", 300)
        self.coil_enable = m.getint("EnableCoil", 400)
        self.mb = ModbusTcp(m.get("Host"), m.getint("Port", 502),
                            m.getint("UnitId", 180), m.getfloat("TimeoutSeconds", 2.0))

        st = load_state()
        self.total_kwh = float(st.get("total_kwh", 0.0))
        self.session_kwh = float(st.get("session_kwh", 0.0))
        self.charging_time = float(st.get("charging_time", 0.0))
        self._saved_total = self.total_kwh
        self._last_save = time.time()

        self.desired_enable = None
        self.desired_current = None
        self.failures = 0
        self.last_update = None
        self.last_letter = None

        instance = d.getint("DeviceInstance", 60)
        svc_name = "com.victronenergy.evcharger.phoenix_%d" % instance
        try:
            svc = dbus_service_cls(svc_name, register=False)
            late_register = True
        except TypeError:  # older velib_python without register kwarg
            svc = dbus_service_cls(svc_name)
            late_register = False
        self.svc = svc

        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", VERSION + " on Python " + sys.version.split()[0])
        svc.add_path("/Mgmt/Connection", "Modbus TCP %s unit %d" % (self.mb.host, self.mb.unit))
        svc.add_path("/DeviceInstance", instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", "Phoenix Contact EV Charger")
        svc.add_path("/CustomName", d.get("CustomName", "Laadpaal"), writeable=True)
        svc.add_path("/FirmwareVersion", VERSION)
        svc.add_path("/HardwareVersion", "EM-CP-PP-ETH")
        svc.add_path("/Serial", "")
        svc.add_path("/Connected", 0)
        svc.add_path("/Position", d.getint("Position", 0), writeable=True)
        svc.add_path("/Model", "EM-CP-PP-ETH")

        _w = lambda p, v: "%.0f W" % v if v is not None else ""
        _a = lambda p, v: "%.0f A" % v if v is not None else ""
        _k = lambda p, v: "%.2f kWh" % v if v is not None else ""
        _s = lambda p, v: "%d s" % v if v is not None else ""

        svc.add_path("/Ac/Power", 0, gettextcallback=_w)
        svc.add_path("/Ac/L1/Power", 0, gettextcallback=_w)
        svc.add_path("/Ac/L2/Power", 0, gettextcallback=_w)
        svc.add_path("/Ac/L3/Power", 0, gettextcallback=_w)
        svc.add_path("/Ac/Energy/Forward", self._energy_value(), gettextcallback=_k)
        svc.add_path("/Current", 0, gettextcallback=_a)
        svc.add_path("/MaxCurrent", self.max_current, gettextcallback=_a)
        svc.add_path("/SetCurrent", self.min_current, writeable=True,
                     onchangecallback=self._on_set_current, gettextcallback=_a)
        svc.add_path("/StartStop", 0, writeable=True, onchangecallback=self._on_start_stop)
        svc.add_path("/Mode", 0, writeable=True, onchangecallback=self._on_mode)
        svc.add_path("/AutoStart", 0, writeable=True)
        svc.add_path("/Status", ST_DISCONNECTED)
        svc.add_path("/ChargingTime", int(self.charging_time), gettextcallback=_s)
        svc.add_path("/EnableDisplay", 0, writeable=True)
        svc.add_path("/Phoenix/CpState", "")

        if late_register:
            svc.register()

        logging.info("started %s (v%s), polling %s:%d unit %d every %d ms",
                     svc_name, VERSION, self.mb.host, self.mb.port, self.mb.unit, self.poll_ms)

    # ---- helpers ---------------------------------------------------------
    def _energy_value(self):
        v = self.session_kwh if self.energy_mode == "session" else self.total_kwh
        return round(v, 3)

    def _clamp_current(self, value):
        return max(self.min_current, min(self.max_current, int(round(float(value)))))

    # ---- D-Bus write callbacks -------------------------------------------
    def _on_start_stop(self, path, value):
        try:
            enable = bool(int(value))
            self.mb.write_coil(self.coil_enable, enable)
        except (ValueError, ModbusError) as e:
            logging.error("StartStop write failed: %s", e)
            return False
        self.desired_enable = enable
        logging.info("StartStop -> %d", enable)
        return True

    def _on_set_current(self, path, value):
        try:
            amps = self._clamp_current(value)
            self.mb.write_register(self.reg_current, amps)
        except (ValueError, ModbusError) as e:
            logging.error("SetCurrent write failed: %s", e)
            return False
        self.desired_current = amps
        logging.info("SetCurrent -> %d A", amps)
        return True

    def _on_mode(self, path, value):
        # Only Manual (0) is supported
        return int(value) == 0

    # ---- polling ---------------------------------------------------------
    def update(self):
        try:
            self._update()
        except Exception:  # never let the GLib timer die
            logging.exception("unexpected error in update")
        return True

    def _update(self):
        now = time.time()
        try:
            raw = self.mb.read_input(self.reg_status)[0]
            enabled = self.mb.read_coils(self.coil_enable)[0]
            setpoint = self.mb.read_holding(self.reg_current)[0]
        except ModbusError as e:
            self.failures += 1
            if self.failures == 1 or self.failures % 30 == 0:
                logging.warning("read failed (%d): %s", self.failures, e)
            if self.failures >= self.fail_limit:
                self._publish_offline()
            self.last_update = None
            return

        if self.failures:
            logging.info("communication restored after %d failures", self.failures)
        self.failures = 0

        # First good read: adopt what the charger has, don't override it
        if self.desired_enable is None:
            self.desired_enable = enabled
        if self.desired_current is None:
            self.desired_current = self._clamp_current(setpoint) if setpoint else self.min_current

        # Re-assert desired values if the charger lost them (e.g. after its own reboot)
        try:
            if enabled != self.desired_enable:
                logging.info("re-asserting enable=%d (charger reports %d)", self.desired_enable, enabled)
                self.mb.write_coil(self.coil_enable, self.desired_enable)
                enabled = self.desired_enable
            if setpoint != self.desired_current:
                logging.info("re-asserting current=%d A (charger reports %d A)", self.desired_current, setpoint)
                self.mb.write_register(self.reg_current, self.desired_current)
                setpoint = self.desired_current
        except ModbusError as e:
            logging.warning("re-assert failed: %s", e)

        letter = chr(raw) if 65 <= raw <= 70 else "?"
        if letter != self.last_letter:
            logging.info("CP state %s -> %s", self.last_letter, letter)
            self.last_letter = letter

        charging = enabled and letter in ("C", "D")
        amps = round(float(setpoint) * self.current_factor, 2) if charging else 0.0
        p_phase = amps * self.voltage
        p_l1 = p_phase
        p_l2 = p_phase if self.phases == 3 else 0.0
        p_l3 = p_phase if self.phases == 3 else 0.0
        p_total = p_l1 + p_l2 + p_l3

        # Integrate calculated energy (skip big gaps, e.g. after comm loss)
        if self.last_update is not None:
            dt = now - self.last_update
            if 0 < dt <= 5 * self.poll_ms / 1000.0:
                kwh = p_total * dt / 3600000.0
                self.total_kwh += kwh
                self.session_kwh += kwh
                if charging:
                    self.charging_time += dt
        self.last_update = now

        # New session when the car is unplugged
        if letter == "A" and (self.session_kwh or self.charging_time):
            self.session_kwh = 0.0
            self.charging_time = 0.0
            self._save(force=True)

        if letter == "A":
            status = ST_DISCONNECTED
        elif charging:
            status = ST_CHARGING
        elif letter == "B":
            if not enabled:
                status = ST_WAIT_START
            elif self.session_kwh > 0.01:
                status = ST_CHARGED
            else:
                status = ST_CONNECTED
        elif letter == "E":
            status = ST_CP_SHORTED
        else:
            status = ST_DISCONNECTED

        s = self.svc
        s["/Connected"] = 1
        s["/Phoenix/CpState"] = letter
        s["/Ac/L1/Power"] = round(p_l1)
        s["/Ac/L2/Power"] = round(p_l2)
        s["/Ac/L3/Power"] = round(p_l3)
        s["/Ac/Power"] = round(p_total)
        s["/Current"] = amps
        s["/SetCurrent"] = setpoint
        s["/StartStop"] = int(enabled)
        s["/Status"] = status
        s["/ChargingTime"] = int(self.charging_time)
        s["/Ac/Energy/Forward"] = self._energy_value()

        self._save()

    def _publish_offline(self):
        s = self.svc
        s["/Connected"] = 0
        s["/Ac/Power"] = 0
        s["/Ac/L1/Power"] = 0
        s["/Ac/L2/Power"] = 0
        s["/Ac/L3/Power"] = 0
        s["/Current"] = 0

    def _save(self, force=False):
        # Limit flash writes: at most once per 5 minutes, or on 0.1 kWh change
        now = time.time()
        if force or (now - self._last_save > 300 and self.total_kwh != self._saved_total) \
                or abs(self.total_kwh - self._saved_total) >= 0.1:
            save_state({
                "total_kwh": round(self.total_kwh, 4),
                "session_kwh": round(self.session_kwh, 4),
                "charging_time": round(self.charging_time, 1),
                "saved_at": int(now),
            })
            self._saved_total = self.total_kwh
            self._last_save = now

    def shutdown(self):
        self._save(force=True)
        self.mb.close()


# --------------------------------------------------------------------------
def main():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONFIG_PATH):
        print("config.ini not found at %s" % CONFIG_PATH)
        sys.exit(1)

    logging.basicConfig(
        format="%(levelname)s %(message)s",
        level=getattr(logging, cfg["DEFAULT"].get("Logging", "INFO").upper(), logging.INFO),
    )

    for p in VELIB_PATHS:
        if os.path.isdir(p):
            sys.path.insert(1, p)
            break
    from vedbus import VeDbusService  # noqa: E402
    from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
    from gi.repository import GLib  # noqa: E402

    DBusGMainLoop(set_as_default=True)
    charger = PhoenixEvCharger(cfg, VeDbusService)
    GLib.timeout_add(charger.poll_ms, charger.update)

    loop = GLib.MainLoop()
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: (loop.quit(), False)[1])
        except AttributeError:
            signal.signal(sig, lambda *a: loop.quit())
    try:
        loop.run()
    finally:
        charger.shutdown()


if __name__ == "__main__":
    main()
