# -*- coding: utf-8 -*-
"""
Minimal OCPP 1.6J charge point client for dbus-phoenix-evcharger.

Reports charging sessions with the (MID) meter readings to a central system
such as Joulo. It never controls the charger: remote start/stop are rejected.

- WebSocket (ws:// and wss:// with TLS) implemented with the standard library
- BootNotification, Heartbeat, StatusNotification
- StartTransaction / MeterValues / StopTransaction with meter readings in Wh
- Transaction messages are queued and persisted, so sessions survive an
  internet outage or a restart and are delivered in order afterwards
- Runs in its own thread; the driver hands it a snapshot every poll
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

log = logging.getLogger("ocpp")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
QUEUED_ACTIONS = ("StartTransaction", "MeterValues", "StopTransaction")
MAX_QUEUE = 20000


def utc_iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_url(url):
    """URL with the auth key hidden, for logging."""
    parts = urlsplit(url)
    q = [(k, "***" if k.lower() in ("auth", "key", "token", "password") and v else v)
         for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    netloc = parts.netloc.split("@")[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(q, safe="*"), ""))


def build_url(url, auth_key):
    url = url.strip()
    if not auth_key:
        return url
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "auth"]
    q.append(("auth", auth_key.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


# --------------------------------------------------------------------------
# WebSocket client (RFC 6455, text frames only)
# --------------------------------------------------------------------------
class WsClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=15.0, subprotocol="ocpp1.6"):
        self.url = url
        self.timeout = timeout
        self.subprotocol = subprotocol
        self.sock = None
        self._frags = b""

    def connect(self):
        parts = urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise WsClosed("URL must start with ws:// or wss://")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        sock = socket.create_connection((host, port), timeout=self.timeout)
        if parts.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        hostport = host if parts.port is None else "%s:%d" % (host, port)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
               "Sec-WebSocket-Protocol: %s\r\n" % (path, hostport, key, self.subprotocol))
        if parts.username:
            cred = "%s:%s" % (parts.username, parts.password or "")
            req += "Authorization: Basic %s\r\n" % base64.b64encode(cred.encode()).decode()
        sock.sendall((req + "\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise WsClosed("connection closed during handshake")
            resp += chunk
            if len(resp) > 16384:
                raise WsClosed("handshake response too large")
        head, _, rest = resp.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            sock.close()
            raise WsClosed("server refused WebSocket: %s" % lines[0])
        hdr = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        expect = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if hdr.get("sec-websocket-accept") != expect:
            sock.close()
            raise WsClosed("invalid Sec-WebSocket-Accept")
        self.sock = sock
        self._buf = rest
        self._frags = b""

    def close(self):
        if self.sock is not None:
            try:
                self._send_frame(0x8, struct.pack(">H", 1000))
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _send_frame(self, opcode, payload):
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def ping(self):
        self._send_frame(0x9, b"")

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WsClosed("connection closed by server")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def recv_text(self, wait):
        """Next text message, or None when nothing arrives within `wait` seconds."""
        deadline = time.time() + wait
        while True:
            if not self._buf:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self.sock.settimeout(remaining)
                try:
                    chunk = self.sock.recv(4096)
                except (socket.timeout, ssl.SSLWantReadError):
                    return None
                finally:
                    self.sock.settimeout(self.timeout)
                if not chunk:
                    raise WsClosed("connection closed by server")
                self._buf += chunk
            b0, b1 = self._read(2)
            opcode, fin = b0 & 0x0F, b0 & 0x80
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise WsClosed("server closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                self._frags += payload
                if fin:
                    text, self._frags = self._frags.decode("utf-8"), b""
                    return text


# --------------------------------------------------------------------------
# OCPP 1.6J charge point
# --------------------------------------------------------------------------
class OcppClient:
    def __init__(self, sec, state_path, info, ws_factory=WebSocket, clock=time.time):
        self.url = build_url(sec.get("Url", ""), sec.get("AuthKey", ""))
        self.id_tag = (sec.get("IdTag", "") or "PHOENIX").strip()[:20]
        self.require_meter = sec.getboolean("RequireMeter", True)
        self.mv_interval = max(10, sec.getint("MeterValueSampleInterval", 60))
        self.hb_interval = max(30, sec.getint("HeartbeatInterval", 300))
        self.state_path = state_path
        self.info = info                    # vendor, model, firmware, meter type
        self.ws_factory = ws_factory
        self.clock = clock

        self._lock = threading.Lock()
        self._snap = None
        self._stop = threading.Event()
        self._thread = None

        self.ws = None
        self.connected = False
        self.accepted = False
        self.last_error = ""
        self._backoff = 10
        self._next_connect = 0.0
        self._pending = {}                  # msg id -> (action, payload, queued)
        self._inflight_queued = None        # msg id of the queue head being sent
        self._last_status = None
        self._last_hb = 0.0
        self._last_mv = 0.0
        self._last_rx = 0.0
        self._boot_retry_at = 0.0
        self._warned_meter = False

        st = self._load()
        self.queue = st.get("queue", [])
        self.tx = st.get("tx")              # active session: {"local": n, "start_wh": .., "start": ts}
        self.tx_map = {str(k): v for k, v in st.get("tx_map", {}).items()}
        self.local_counter = int(st.get("local_counter", 0))

    # ---- persistence ------------------------------------------------------
    def _load(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self):
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"queue": self.queue, "tx": self.tx, "tx_map": self.tx_map,
                           "local_counter": self.local_counter}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("could not save OCPP state: %s", e)

    # ---- interface for the driver ------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="ocpp", daemon=True)
        self._thread.start()
        log.info("OCPP client started for %s", mask_url(self.url))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self._save()
        if self.ws:
            self.ws.close()

    def update(self, snap):
        """Called by the driver every poll with the current state."""
        with self._lock:
            self._snap = dict(snap)

    def status(self):
        with self._lock:
            return {"connected": int(self.connected and self.accepted), "queue": len(self.queue),
                    "tx": self.tx_map.get(str(self.tx["local"])) if self.tx else None,
                    "error": self.last_error}

    # ---- thread -------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self.step(wait=1.0)
            except Exception:
                log.exception("unexpected OCPP error")
                self._disconnect("internal error")
                time.sleep(5)

    def step(self, wait=1.0):
        """One iteration: connect if needed, read messages, generate events, send."""
        now = self.clock()
        with self._lock:
            snap = dict(self._snap) if self._snap else None
        if snap:
            self._events(snap, now)
        if not self.connected:
            if now >= self._next_connect and self.url:
                self._connect(now)
            if not self.connected:
                if wait:
                    time.sleep(min(wait, 1.0))
                return
        try:
            self._send_due(snap, now)
            msg = self.ws.recv_text(wait)
            while msg is not None:
                self._last_rx = self.clock()
                self._handle(msg)
                msg = self.ws.recv_text(0)
            self._check_timeouts(self.clock())
        except (OSError, WsClosed, ssl.SSLError, ValueError) as e:
            self._disconnect(str(e))

    # ---- connection ---------------------------------------------------------
    def _connect(self, now):
        ws = self.ws_factory(self.url)
        try:
            ws.connect()
        except Exception as e:
            self.last_error = str(e)
            if self._backoff == 10 or self._backoff >= 300:
                log.warning("OCPP connect to %s failed: %s (retry in %d s)", mask_url(self.url), e, self._backoff)
            self._next_connect = now + self._backoff
            self._backoff = min(self._backoff * 2, 300)
            return
        self.ws = ws
        self.connected = True
        self.accepted = False
        self.last_error = ""
        self._backoff = 10
        self._pending = {}
        self._inflight_queued = None
        self._last_status = None
        self._last_rx = now
        log.info("OCPP connected to %s", mask_url(self.url))
        self._boot(now)

    def _disconnect(self, reason):
        if self.connected:
            log.warning("OCPP disconnected: %s", reason)
        self.last_error = reason
        if self.ws:
            self.ws.close()
        self.ws = None
        self.connected = False
        self.accepted = False
        self._pending = {}
        self._inflight_queued = None
        self._next_connect = self.clock() + self._backoff
        self._backoff = min(self._backoff * 2, 300)

    def _call(self, action, payload, queued=False):
        mid = uuid.uuid4().hex[:20]
        self._pending[mid] = (action, payload, queued, self.clock())
        self.ws.send_text(json.dumps([2, mid, action, payload], separators=(",", ":")))
        if queued:
            self._inflight_queued = mid
        return mid

    def _reply(self, mid, payload):
        self.ws.send_text(json.dumps([3, mid, payload], separators=(",", ":")))

    def _reply_error(self, mid, code, desc):
        self.ws.send_text(json.dumps([4, mid, code, desc, {}], separators=(",", ":")))

    def _boot(self, now):
        p = {"chargePointVendor": self.info.get("vendor", "Phoenix Contact")[:20],
             "chargePointModel": self.info.get("model", "EM-CP-PP-ETH")[:20],
             "firmwareVersion": str(self.info.get("firmware", ""))[:50]}
        if self.info.get("meter_type"):
            p["meterType"] = self.info["meter_type"][:25]
        serial = (self._snap or {}).get("meter_serial") or self.info.get("meter_serial")
        if serial:
            p["meterSerialNumber"] = str(serial)[:25]
        self._call("BootNotification", p)
        self._boot_retry_at = now + 60

    # ---- incoming -----------------------------------------------------------
    def _handle(self, text):
        msg = json.loads(text)
        kind = msg[0]
        if kind == 2:
            self._handle_call(msg[1], msg[2], msg[3] if len(msg) > 3 else {})
        elif kind in (3, 4):
            entry = self._pending.pop(msg[1], None)
            if entry is None:
                return
            action, payload, queued, _ = entry
            if kind == 4:
                log.warning("OCPP %s rejected by server: %s %s", action, msg[2], msg[3] if len(msg) > 3 else "")
                if queued:
                    self._inflight_queued = None
                    # a malformed queued message would block the queue forever: drop it
                    if msg[2] in ("FormationViolation", "PropertyConstraintViolation", "TypeConstraintViolation",
                                  "ProtocolError", "NotImplemented", "NotSupported"):
                        with self._lock:
                            if self.queue:
                                self.queue.pop(0)
                                self._save()
                return
            self._result(action, payload, queued, msg[2] if len(msg) > 2 else {})

    def _result(self, action, payload, queued, res):
        if action == "BootNotification":
            status = res.get("status")
            interval = int(res.get("interval") or 0)
            if status == "Accepted":
                self.accepted = True
                if interval >= 30:
                    self.hb_interval = interval
                self._last_hb = self.clock()
                log.info("OCPP BootNotification accepted, heartbeat %d s, %d queued messages",
                         self.hb_interval, len(self.queue))
            else:
                self._boot_retry_at = self.clock() + max(interval, 30)
                log.warning("OCPP BootNotification %s, retry in %d s", status, max(interval, 30))
            return
        if queued:
            self._inflight_queued = None
            with self._lock:
                head = self.queue.pop(0) if self.queue else None
                if head and head["action"] == "StartTransaction":
                    tid = res.get("transactionId")
                    self.tx_map[str(head["local"])] = tid
                    status = (res.get("idTagInfo") or {}).get("status")
                    log.info("OCPP StartTransaction: transactionId %s (idTag %s)", tid, status)
                elif head and head["action"] == "StopTransaction":
                    log.info("OCPP StopTransaction delivered (transaction %s)", payload.get("transactionId"))
                    self.tx_map.pop(str(head["local"]), None)
                if head and (head["action"] != "MeterValues" or len(self.queue) == 0 or len(self.queue) % 50 == 0):
                    self._save()

    def _handle_call(self, mid, action, payload):
        cfg = {"HeartbeatInterval": str(self.hb_interval),
               "MeterValueSampleInterval": str(self.mv_interval),
               "MeterValuesSampledData": "Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage",
               "NumberOfConnectors": "1",
               "AuthorizeRemoteTxRequests": "false",
               "StopTransactionOnEVSideDisconnect": "true",
               "ConnectionTimeOut": "0",
               "LocalPreAuthorize": "false",
               "SupportedFeatureProfiles": "Core,RemoteTrigger"}
        if action == "GetConfiguration":
            keys = payload.get("key") or list(cfg)
            known = [{"key": k, "readonly": k not in ("HeartbeatInterval", "MeterValueSampleInterval"),
                      "value": cfg[k]} for k in keys if k in cfg]
            unknown = [k for k in keys if k not in cfg]
            res = {"configurationKey": known}
            if unknown:
                res["unknownKey"] = unknown
            self._reply(mid, res)
        elif action == "ChangeConfiguration":
            key, value = payload.get("key"), str(payload.get("value", ""))
            if key in ("HeartbeatInterval", "MeterValueSampleInterval") and value.isdigit():
                if key == "HeartbeatInterval":
                    self.hb_interval = max(30, int(value))
                else:
                    self.mv_interval = max(10, int(value))
                log.info("OCPP %s set to %s by server", key, value)
                self._reply(mid, {"status": "Accepted"})
            elif key in cfg:
                self._reply(mid, {"status": "Rejected"})
            else:
                self._reply(mid, {"status": "NotSupported"})
        elif action == "TriggerMessage":
            req = payload.get("requestedMessage")
            if req in ("BootNotification", "Heartbeat", "StatusNotification", "MeterValues"):
                self._reply(mid, {"status": "Accepted"})
                if req == "BootNotification":
                    self._boot(self.clock())
                elif req == "Heartbeat":
                    self._call("Heartbeat", {})
                elif req == "StatusNotification":
                    self._last_status = None
                elif req == "MeterValues":
                    self._last_mv = 0.0
            else:
                self._reply(mid, {"status": "NotImplemented"})
        elif action in ("RemoteStartTransaction", "RemoteStopTransaction", "Reset", "ChangeAvailability"):
            # this client only reports; charging is controlled on the GX
            log.info("OCPP %s from server rejected (control stays on the GX)", action)
            self._reply(mid, {"status": "Rejected"})
        elif action == "UnlockConnector":
            self._reply(mid, {"status": "NotSupported"})
        elif action == "ClearCache":
            self._reply(mid, {"status": "Accepted"})
        elif action == "DataTransfer":
            self._reply(mid, {"status": "UnknownVendorId"})
        else:
            self._reply_error(mid, "NotImplemented", "%s not supported" % action)

    # ---- events from the charger state -------------------------------------
    def _energy_wh(self, snap):
        if snap.get("meter_kwh") is not None:
            return int(round(snap["meter_kwh"] * 1000))
        if not self.require_meter and snap.get("calc_kwh") is not None:
            return int(round(snap["calc_kwh"] * 1000))
        return None

    def _enqueue(self, action, payload, local):
        with self._lock:
            self.queue.append({"action": action, "payload": payload, "local": local})
            if len(self.queue) > MAX_QUEUE:
                for i, item in enumerate(self.queue):
                    if item["action"] == "MeterValues":
                        del self.queue[i]
                        break
            if action != "MeterValues" or len(self.queue) > 1:
                self._save()

    def _sampled(self, snap, wh, context):
        sv = [{"value": str(wh), "context": context, "measurand": "Energy.Active.Import.Register",
               "location": "Outlet", "unit": "Wh"}]
        if snap.get("power") is not None:
            sv.append({"value": "%.0f" % snap["power"], "context": context, "measurand": "Power.Active.Import",
                       "location": "Outlet", "unit": "W"})
        for i, ph in enumerate(("L1", "L2", "L3")):
            if snap.get("currents") and snap["currents"][i] is not None:
                sv.append({"value": "%.2f" % snap["currents"][i], "context": context, "measurand": "Current.Import",
                           "phase": ph, "location": "Outlet", "unit": "A"})
            if snap.get("voltages") and snap["voltages"][i] is not None:
                sv.append({"value": "%.1f" % snap["voltages"][i], "context": context, "measurand": "Voltage",
                           "phase": ph + "-N", "location": "Outlet", "unit": "V"})
        return sv

    def _events(self, snap, now):
        if not snap.get("charger_ok"):
            return
        letter = snap.get("letter")
        t = snap.get("time", now)
        plugged = letter in ("B", "C", "D")
        wh = self._energy_wh(snap)

        if plugged and self.tx is None:
            if wh is None:
                if not self._warned_meter:
                    log.warning("OCPP: car connected but no meter reading - session not started (RequireMeter=1)")
                    self._warned_meter = True
            else:
                self._warned_meter = False
                self.local_counter += 1
                self.tx = {"local": self.local_counter, "start_wh": wh, "start": t}
                self._enqueue("StartTransaction", {"connectorId": 1, "idTag": self.id_tag, "meterStart": wh,
                                                   "timestamp": utc_iso(t)}, self.local_counter)
                self._last_mv = now
                log.info("OCPP session %d started at %d Wh", self.local_counter, wh)
        elif not plugged and letter == "A" and self.tx is not None:
            stop_wh = wh if wh is not None else snap.get("last_meter_wh") or self.tx["start_wh"]
            stop_wh = max(stop_wh, self.tx["start_wh"])
            local = self.tx["local"]
            self._enqueue("StopTransaction", {"meterStop": stop_wh, "timestamp": utc_iso(t), "reason": "EVDisconnected",
                                              "idTag": self.id_tag}, local)
            log.info("OCPP session %d stopped at %d Wh (%.3f kWh)", local, stop_wh,
                     (stop_wh - self.tx["start_wh"]) / 1000.0)
            self.tx = None
            with self._lock:
                self._save()
        elif self.tx is not None and wh is not None and now - self._last_mv >= self.mv_interval:
            self._last_mv = now
            self._enqueue("MeterValues", {"connectorId": 1, "meterValue": [
                {"timestamp": utc_iso(t), "sampledValue": self._sampled(snap, wh, "Sample.Periodic")}]},
                self.tx["local"])

    def _connector_status(self, snap):
        letter = snap.get("letter")
        if not snap.get("charger_ok"):
            return "Unavailable", "NoError"
        if letter in ("E", "F"):
            return "Faulted", "OtherError"
        if letter in ("C", "D") and snap.get("enabled"):
            return "Charging", "NoError"
        if letter in ("B", "C", "D"):
            if self.tx is None:
                return "Preparing", "NoError"
            return ("SuspendedEV" if snap.get("enabled") else "SuspendedEVSE"), "NoError"
        return "Available", "NoError"

    # ---- outgoing -----------------------------------------------------------
    def _send_due(self, snap, now):
        if not self.accepted:
            if now >= self._boot_retry_at and not any(a == "BootNotification" for a, *_ in self._pending.values()):
                self._boot(now)
            return
        if snap:
            st = self._connector_status(snap)
            if st != self._last_status:
                self._call("StatusNotification", {"connectorId": 1, "status": st[0], "errorCode": st[1],
                                                  "timestamp": utc_iso(snap.get("time", now))})
                self._last_status = st
        if now - self._last_hb >= self.hb_interval:
            self._call("Heartbeat", {})
            self._last_hb = now
        if self._inflight_queued is None:
            with self._lock:
                head = self.queue[0] if self.queue else None
            if head:
                payload = dict(head["payload"])
                if head["action"] in ("MeterValues", "StopTransaction"):
                    tid = self.tx_map.get(str(head["local"]))
                    if tid is None:
                        # its StartTransaction got no id (rejected): don't block the queue
                        log.warning("OCPP: no transactionId for session %s, dropping %s", head["local"], head["action"])
                        with self._lock:
                            self.queue.pop(0)
                            self._save()
                        return
                    payload["transactionId"] = tid
                self._call(head["action"], payload, queued=True)

    def _check_timeouts(self, now):
        for mid, (action, _, queued, sent) in list(self._pending.items()):
            if now - sent > 60:
                raise WsClosed("no answer to %s within 60 s" % action)
        if now - self._last_rx > max(2 * self.hb_interval, 600):
            raise WsClosed("nothing received for %d s" % (now - self._last_rx))
