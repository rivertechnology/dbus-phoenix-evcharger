#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dbus-phoenix-evcharger

Minimal Venus OS driver for a Phoenix Contact EM-CP-PP-ETH charge controller
over Modbus TCP, published as com.victronenergy.evcharger.

- Manual mode (start/stop + charge current from the GX GUI) and Auto mode (PV surplus)
- Optional Eastron SDM72D-M (MID) meter via a Modbus TCP gateway for real
  power, current, voltage and kWh; without it power and energy are CALCULATED
  as setpoint current x CurrentFactor x voltage x phases in state C/D
- Total energy counter persisted in /data so it survives restarts
- No external Python dependencies (own minimal Modbus TCP client)

Written with Claude (Anthropic). License: MIT.
"""

import configparser
import json
import logging
import os
import socket
import struct
import sys
import time

VERSION = "1.3.0"
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
ST_WAIT_SUN = 4
ST_WAIT_START = 6
ST_LOW_SOC = 7

MODE_MANUAL = 0
MODE_AUTO = 1
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
# Eastron SDM72D-M energy meter (input registers, float32), via Modbus TCP gateway
# --------------------------------------------------------------------------
class Sdm72Meter:
    def __init__(self, sec):
        self.mb = ModbusTcp(sec.get("Host").strip(), sec.getint("Port", 502),
                            sec.getint("UnitId", 1), sec.getfloat("TimeoutSeconds", 2.0))
        self.swap_words = sec.get("WordOrder", "big").strip().lower() == "little"
        self.reg_phase = sec.getint("PhaseBlockRegister", 0)      # V1..3, I1..3, P1..3
        self.reg_total_p = sec.getint("TotalPowerRegister", 52)
        self.reg_energy = sec.getint("EnergyRegister", 72)        # total import kWh
        self.last_kwh = None

    def _floats(self, addr, count):
        regs = self.mb.read_input(addr, 2 * count)
        out = []
        for i in range(count):
            hi, lo = regs[2 * i], regs[2 * i + 1]
            if self.swap_words:
                hi, lo = lo, hi
            v = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
            if v != v or v in (float("inf"), float("-inf")):
                raise ModbusError("meter returned NaN/inf at register %d" % (addr + 2 * i))
            out.append(v)
        return out

    def read(self):
        ph = self._floats(self.reg_phase, 9)
        if not any(100.0 <= v <= 300.0 for v in ph[0:3]):
            raise ModbusError("implausible voltages %s - check UnitId, WordOrder and registers"
                              % ", ".join("%.4g" % v for v in ph[0:3]))
        total_p = self._floats(self.reg_total_p, 1)[0]
        kwh = self._floats(self.reg_energy, 1)[0]
        if kwh < 0 or (self.last_kwh is not None and kwh < self.last_kwh - 0.01):
            raise ModbusError("implausible energy reading %.3f kWh (last %.3f)" % (kwh, self.last_kwh or 0))
        self.last_kwh = kwh
        return {"v": ph[0:3], "i": ph[3:6], "p": ph[6:9], "p_total": total_p, "kwh": kwh}

    def close(self):
        self.mb.close()


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
# Reads values of other Venus services (grid, battery) from D-Bus
# --------------------------------------------------------------------------
class SystemReader:
    def __init__(self, bus):
        self.bus = bus

    def get(self, path, service="com.victronenergy.system"):
        try:
            obj = self.bus.get_object(service, path, introspect=False)
            value = obj.GetValue(dbus_interface="com.victronenergy.BusItem", timeout=1.0)
            return float(value)  # invalid values are empty arrays -> TypeError
        except Exception:
            return None


# --------------------------------------------------------------------------
# Charger service
# --------------------------------------------------------------------------
class PhoenixEvCharger:
    def __init__(self, cfg, dbus_service_cls, system_reader=None):
        d = cfg["DEFAULT"]
        self.poll_ms = d.getint("PollIntervalMs", 2000)
        self.voltage = d.getfloat("Voltage", 230.0)
        self.phases = d.getint("Phases", 3)
        self.current_factor = d.getfloat("CurrentFactor", 1.0)
        self.min_current = d.getint("MinCurrent", 6)
        self.max_current = d.getint("MaxCurrent", 16)
        self.default_current = d.getint("DefaultCurrent", 10)
        self.energy_mode = d.get("EnergyForward", "total").strip().lower()
        self.fail_limit = d.getint("FailuresBeforeDisconnect", 3)

        m = cfg["MODBUS"]
        self.reg_status = m.getint("StatusInputRegister", 100)
        self.reg_current = m.getint("CurrentHoldingRegister", 300)
        self.coil_enable = m.getint("EnableCoil", 400)
        self.mb = ModbusTcp(m.get("Host"), m.getint("Port", 502),
                            m.getint("UnitId", 180), m.getfloat("TimeoutSeconds", 2.0))

        mt = cfg["METER"] if cfg.has_section("METER") else None
        self.meter = Sdm72Meter(mt) if mt is not None and mt.get("Host", "").strip() else None
        self.meter_failures = 0
        self.meter_ok = False

        st = load_state()
        self.session_start_kwh = st.get("session_start_kwh")
        self.total_kwh = float(st.get("total_kwh", 0.0))
        self.session_kwh = float(st.get("session_kwh", 0.0))
        self.charging_time = float(st.get("charging_time", 0.0))
        self._saved_total = self.total_kwh
        self._last_save = time.time()

        a = cfg["AUTO"] if cfg.has_section("AUTO") else cfg["DEFAULT"]
        self.auto_start_w = a.getfloat("StartSurplusWatts", 0)  # 0 = minimum charge power + 300 W
        self.auto_stop_import_w = a.getfloat("StopImportWatts", 500)
        self.auto_start_delay = a.getfloat("StartDelaySeconds", 60)
        self.auto_stop_delay = a.getfloat("StopDelaySeconds", 600)
        self.auto_min_runtime = a.getfloat("MinRuntimeSeconds", 900)
        self.auto_min_offtime = a.getfloat("MinOfftimeSeconds", 600)
        self.auto_adjust_interval = a.getfloat("AdjustIntervalSeconds", 60)
        self.auto_alpha = a.getfloat("SmoothingAlpha", 0.3)
        self.auto_use_battery = a.getboolean("UseBattery", True)
        self.auto_min_soc = a.getfloat("MinSoc", 40)
        self.system = system_reader

        # Manual charge current: last value chosen in the GUI, else DefaultCurrent
        self.manual_current = self._clamp_current(st.get("manual_current", self.default_current))
        self.mode = MODE_AUTO if int(st.get("mode", MODE_MANUAL)) == MODE_AUTO else MODE_MANUAL
        self._avail = None
        self._start_since = None
        self._stop_since = None
        self._on_since = 0.0
        self._off_since = 0.0
        self._last_adjust = 0.0
        self.auto_reason = ""
        self._low_soc = False

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
        conn = "Modbus TCP %s unit %d" % (self.mb.host, self.mb.unit)
        if self.meter:
            conn += ", SDM72D-M %s unit %d" % (self.meter.mb.host, self.meter.mb.unit)
        svc.add_path("/Mgmt/Connection", conn)
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
        _v = lambda p, v: "%.1f V" % v if v is not None else ""
        _a1 = lambda p, v: "%.1f A" % v if v is not None else ""
        for i in (1, 2, 3):
            svc.add_path("/Ac/L%d/Voltage" % i, None, gettextcallback=_v)
            svc.add_path("/Ac/L%d/Current" % i, None, gettextcallback=_a1)
        svc.add_path("/Meter/Connected", 0)
        svc.add_path("/Meter/Source", "SDM72D-M" if self.meter else "calculated")
        svc.add_path("/Current", 0, gettextcallback=_a)
        svc.add_path("/MaxCurrent", self.max_current, gettextcallback=_a)
        svc.add_path("/SetCurrent", self.min_current, writeable=True,
                     onchangecallback=self._on_set_current, gettextcallback=_a)
        svc.add_path("/StartStop", 0, writeable=True, onchangecallback=self._on_start_stop)
        svc.add_path("/Mode", self.mode, writeable=True, onchangecallback=self._on_mode)
        svc.add_path("/Auto/AvailableWatts", None, gettextcallback=_w)
        svc.add_path("/Auto/Reason", "")
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
        if self.mode == MODE_AUTO:
            logging.info("StartStop ignored: Auto mode is active")
            return False
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
        if self.mode == MODE_AUTO:
            logging.info("SetCurrent ignored: Auto mode is active")
            return False
        try:
            amps = self._clamp_current(value)
            self.mb.write_register(self.reg_current, amps)
        except (ValueError, ModbusError) as e:
            logging.error("SetCurrent write failed: %s", e)
            return False
        self.desired_current = amps
        self.manual_current = amps
        logging.info("SetCurrent -> %d A", amps)
        self._save(force=True)
        return True

    def _on_mode(self, path, value):
        try:
            mode = int(value)
        except (TypeError, ValueError):
            return False
        if mode not in (MODE_MANUAL, MODE_AUTO):
            return False  # Scheduled (2) not supported
        if mode == MODE_AUTO and self.system is None:
            logging.error("Auto mode unavailable: no system reader")
            return False
        if mode != self.mode:
            now = time.time()
            self.mode = mode
            self._avail = None
            self._start_since = None
            self._stop_since = None
            if self.desired_enable:
                self._on_since = now
            if mode == MODE_MANUAL:
                self.desired_current = self.manual_current
            self.auto_reason = ""
            logging.info("Mode -> %s", "Auto" if mode == MODE_AUTO else "Manual")
            self._save(force=True)
        return True

    # ---- Auto mode (PV surplus) ------------------------------------------
    def _read_surplus(self, charge_w):
        """Available power for the car in W (incl. what it already uses), or None."""
        get = self.system.get
        self._low_soc = False
        phases = [get("/Ac/Grid/L%d/Power" % i) for i in (1, 2, 3)]
        phases = [p for p in phases if p is not None]
        if not phases:
            return None
        available = -sum(phases) + charge_w
        if self.auto_use_battery:
            batt_p = get("/Dc/Battery/Power")
            soc = get("/Dc/Battery/Soc")
            self._low_soc = soc is not None and soc < self.auto_min_soc
            if batt_p is not None:
                # Battery charging counts as surplus only above MinSoc;
                # battery discharging always counts against the car.
                available += min(batt_p, 0.0) if self._low_soc else batt_p
        return available

    def _auto_control(self, letter, enabled, charge_w, now):
        if letter not in ("B", "C", "D"):
            if self.desired_enable:
                logging.info("Auto: car disconnected, disabling")
                self.desired_enable = False
                self._off_since = now
            self._avail = None
            self._start_since = self._stop_since = None
            self.auto_reason = "no car"
            self.svc["/Auto/AvailableWatts"] = None
            return

        available = self._read_surplus(charge_w)
        w_per_amp = self.voltage * self.phases * self.current_factor
        min_w = self.min_current * w_per_amp

        if available is None:
            self._avail = None
            self.svc["/Auto/AvailableWatts"] = None
            self.auto_reason = "no grid data"
            if enabled:
                self._stop_since = self._stop_since or now
                if now - self._stop_since >= self.auto_stop_delay:
                    logging.warning("Auto: no grid data, stopping")
                    self.desired_enable = False
                    self._off_since = now
            return

        a = self.auto_alpha
        self._avail = available if self._avail is None else a * available + (1 - a) * self._avail
        avg = self._avail
        self.svc["/Auto/AvailableWatts"] = round(avg)

        if not enabled:
            start_w = self.auto_start_w or (min_w + 300)
            if self._low_soc:
                self.auto_reason = "battery below MinSoc"
                self._start_since = None
            elif now - self._off_since < self.auto_min_offtime:
                self.auto_reason = "min off-time"
                self._start_since = None
            elif avg < start_w:
                self.auto_reason = "waiting for %d W surplus" % start_w
                self._start_since = None
            else:
                self._start_since = self._start_since or now
                self.auto_reason = "starting"
                if now - self._start_since >= self.auto_start_delay:
                    amps = self._clamp_current(int(avg // w_per_amp))
                    logging.info("Auto: start, surplus %d W -> %d A", avg, amps)
                    self.desired_current = amps
                    self.desired_enable = True
                    self._on_since = self._last_adjust = now
                    self._start_since = None
            return

        # Charging: follow the surplus, stop after a sustained deficit
        target = int(avg // w_per_amp)
        if target >= self.min_current and not self._low_soc:
            self._stop_since = None
            new = self._clamp_current(target)
            self.auto_reason = "following surplus"
            if new != self.desired_current and now - self._last_adjust >= self.auto_adjust_interval:
                logging.info("Auto: adjust %s -> %d A (surplus %d W)", self.desired_current, new, avg)
                self.desired_current = new
                self._last_adjust = now
            return

        if self.desired_current != self.min_current:
            self.desired_current = self.min_current
            self._last_adjust = now
        deficit = min_w - avg
        if deficit > self.auto_stop_import_w or self._low_soc:
            self._stop_since = self._stop_since or now
            self.auto_reason = "low surplus" if not self._low_soc else "battery below MinSoc"
            if now - self._stop_since >= self.auto_stop_delay and now - self._on_since >= self.auto_min_runtime:
                logging.info("Auto: stop, surplus %d W (%s)", avg, self.auto_reason)
                self.desired_enable = False
                self._off_since = now
                self._stop_since = None
        else:
            self._stop_since = None
            self.auto_reason = "minimum current"

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

        # First good read: keep the charger's on/off state, set the manual current
        if self.desired_enable is None:
            self.desired_enable = enabled
        if self.desired_current is None:
            self.desired_current = self.manual_current

        letter = chr(raw) if 65 <= raw <= 70 else "?"
        meas = self._read_meter()
        if self.mode == MODE_AUTO:
            if meas is not None:
                cur_w = max(meas["p_total"], 0.0)
            else:
                cur_w = setpoint * self.current_factor * self.voltage * self.phases \
                    if (enabled and letter in ("C", "D")) else 0.0
            self._auto_control(letter, enabled, cur_w, now)
            self.svc["/Auto/Reason"] = self.auto_reason
        else:
            self.svc["/Auto/Reason"] = ""

        # Re-assert desired values if the charger lost them (e.g. after its own reboot)
        try:
            if enabled != self.desired_enable:
                logging.info("write enable=%d (charger had %d)", self.desired_enable, enabled)
                self.mb.write_coil(self.coil_enable, self.desired_enable)
                enabled = self.desired_enable
            if setpoint != self.desired_current:
                logging.info("write current=%d A (charger had %d A)", self.desired_current, setpoint)
                self.mb.write_register(self.reg_current, self.desired_current)
                setpoint = self.desired_current
        except ModbusError as e:
            logging.warning("write failed: %s", e)

        if letter != self.last_letter:
            logging.info("CP state %s -> %s", self.last_letter, letter)
            self.last_letter = letter

        charging = enabled and letter in ("C", "D")
        if meas is not None:
            p_l1, p_l2, p_l3 = meas["p"]
            p_total = meas["p_total"]
            amps = round(max(meas["i"]), 2)
        else:
            amps = round(float(setpoint) * self.current_factor, 2) if charging else 0.0
            p_phase = amps * self.voltage
            p_l1 = p_phase
            p_l2 = p_phase if self.phases == 3 else 0.0
            p_l3 = p_phase if self.phases == 3 else 0.0
            p_total = p_l1 + p_l2 + p_l3

        dt = None
        if self.last_update is not None:
            dt = now - self.last_update
            if not 0 < dt <= 5 * self.poll_ms / 1000.0:
                dt = None  # skip big gaps, e.g. after comm loss
        self.last_update = now
        if dt and charging:
            self.charging_time += dt

        if meas is not None:
            # Energy straight from the MID meter
            kwh = meas["kwh"]
            if self.session_start_kwh is None or self.session_start_kwh > kwh:
                self.session_start_kwh = kwh - (self.session_kwh if self.session_kwh <= kwh else 0.0)
            if abs(self.total_kwh - kwh) > 0.5:
                logging.info("energy counter now from meter: %.2f kWh (was %.2f)", kwh, self.total_kwh)
            self.total_kwh = kwh
            self.session_kwh = max(0.0, kwh - self.session_start_kwh)
        elif self.meter is None and dt:
            # No meter configured: integrate calculated energy
            kwh = p_total * dt / 3600000.0
            self.total_kwh += kwh
            self.session_kwh += kwh
        # Meter configured but not readable: keep energy frozen until it is back

        # New session when the car is unplugged
        if letter == "A" and (self.session_kwh or self.charging_time):
            self.session_kwh = 0.0
            self.charging_time = 0.0
            if meas is not None:
                self.session_start_kwh = meas["kwh"]
            self._save(force=True)

        if letter == "A":
            status = ST_DISCONNECTED
        elif charging:
            status = ST_CHARGING
        elif letter == "B":
            if not enabled and self.mode == MODE_AUTO:
                status = ST_LOW_SOC if self._low_soc else ST_WAIT_SUN
            elif not enabled:
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
        for i in (1, 2, 3):
            s["/Ac/L%d/Voltage" % i] = round(meas["v"][i - 1], 1) if meas else None
            s["/Ac/L%d/Current" % i] = round(meas["i"][i - 1], 2) if meas else None
        s["/Meter/Connected"] = 1 if meas else 0

        self._save()

    def _read_meter(self):
        if self.meter is None:
            return None
        try:
            meas = self.meter.read()
        except ModbusError as e:
            self.meter_failures += 1
            if self.meter_failures == 1 or self.meter_failures % 30 == 0:
                logging.warning("meter read failed (%d), using calculated power: %s", self.meter_failures, e)
            self.meter_ok = False
            return None
        if not self.meter_ok:
            logging.info("meter OK: %.1f/%.1f/%.1f V, %.0f W, %.3f kWh",
                         meas["v"][0], meas["v"][1], meas["v"][2], meas["p_total"], meas["kwh"])
        self.meter_ok = True
        self.meter_failures = 0
        return meas

    def _publish_offline(self):
        s = self.svc
        s["/Connected"] = 0
        s["/Ac/Power"] = 0
        s["/Ac/L1/Power"] = 0
        s["/Ac/L2/Power"] = 0
        s["/Ac/L3/Power"] = 0
        s["/Current"] = 0
        s["/Meter/Connected"] = 0

    def _save(self, force=False):
        # Limit flash writes: at most once per 5 minutes, or on 0.1 kWh change
        now = time.time()
        if force or (now - self._last_save > 300 and self.total_kwh != self._saved_total) \
                or abs(self.total_kwh - self._saved_total) >= 0.1:
            save_state({
                "total_kwh": round(self.total_kwh, 4),
                "session_kwh": round(self.session_kwh, 4),
                "charging_time": round(self.charging_time, 1),
                "mode": self.mode,
                "manual_current": self.manual_current,
                "session_start_kwh": None if self.session_start_kwh is None else round(self.session_start_kwh, 4),
                "saved_at": int(now),
            })
            self._saved_total = self.total_kwh
            self._last_save = now

    def shutdown(self):
        self._save(force=True)
        self.mb.close()
        if self.meter:
            self.meter.close()


# --------------------------------------------------------------------------
def test_meter(cfg, args):
    """Read the meter once with both word orders: python3 dbus-phoenix-evcharger.py --test-meter [host]"""
    sec = cfg["METER"]
    if args:
        sec["Host"] = args[0]
    if not sec.get("Host", "").strip():
        print("Geen meter: zet Host onder [METER] in config.ini, of geef het IP mee als argument.")
        return 1
    ok = False
    for order in ("big", "little"):
        sec["WordOrder"] = order
        m = Sdm72Meter(sec)
        try:
            r = m.read()
            ok = True
            print("WordOrder=%-6s OK  U=%.1f/%.1f/%.1f V  I=%.2f/%.2f/%.2f A  P=%.0f W  import=%.3f kWh" % (
                order, *r["v"], *r["i"], r["p_total"], r["kwh"]))
        except ModbusError as e:
            print("WordOrder=%-6s --  %s" % (order, e))
        finally:
            m.close()
    return 0 if ok else 1


def main():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONFIG_PATH):
        print("config.ini not found at %s" % CONFIG_PATH)
        sys.exit(1)

    logging.basicConfig(
        format="%(levelname)s %(message)s",
        level=getattr(logging, cfg["DEFAULT"].get("Logging", "INFO").upper(), logging.INFO),
    )

    if len(sys.argv) > 1 and sys.argv[1] == "--test-meter":
        sys.exit(test_meter(cfg, sys.argv[2:]))

    for p in VELIB_PATHS:
        if os.path.isdir(p):
            sys.path.insert(1, p)
            break
    from vedbus import VeDbusService  # noqa: E402
    from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
    from gi.repository import GLib  # noqa: E402

    import dbus  # noqa: E402
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ else dbus.SystemBus()
    charger = PhoenixEvCharger(cfg, VeDbusService, SystemReader(bus))
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
