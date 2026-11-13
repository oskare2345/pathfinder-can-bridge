#!/usr/bin/env python3
"""
Pathfinder CAN bridge - lets Linde Pathfinder talk to a forklift through a generic USB-CAN adapter,
plus read-only diagnostic tools, automatic fault logging and a compatibility report.

Unofficial project, not affiliated with or endorsed by Linde Material Handling / KION Group.
"Linde" and "Pathfinder" are used only to describe what this works with. MIT licence, no warranty.
Developed and tested on a Linde E20 (type 386-02); other trucks: see COMPATIBILITY.md.

Pathfinder talks to a (virtual) serial port as if a Linde diagnostic adapter were attached.
This script plays that adapter: it unpacks Pathfinder's serial frames, sends them on the truck's
CAN bus through a cheap USB-CAN adapter (slcan), and packs the truck's answers back.

    Pathfinder -- COM7 == virtual pair == COM8 -- this bridge -- COM6 (slcan, 500 kbit/s) -- truck

Pathfinder serial frame (all confirmed against the truck):
    02 | cmd | 07 | 8 CAN data bytes | checksum
    - 02 = start byte; any 02 inside the frame is sent doubled (02 02)
    - cmd 0x8n = request to controller n  -> CAN ID 0x780+n
      cmd 0x9n = answer from controller n <- CAN ID 0x790+n
    - 07 = data length - 1
    - checksum = XOR of every wire byte after the start byte
    example: 02 81 07 [02 02] 10 81 FF FF FF FF FF E8 = "10 81" (start session) to controller 1

The CAN data is ISO-TP carrying KWP2000. Pathfinder never sends ISO-TP flow control (the real
adapter does), so the bridge sends "30 00 00" itself when a controller starts a long answer.

Usage:  python pathfinder_can.py gui            (window with port selection and troubleshooting)
        python pathfinder_can.py --help         (all command-line tools)
Needs:  pip install python-can pyserial
"""
import argparse
import csv
import json
import os
import platform
import re
import sys
import threading
import time
from dataclasses import dataclass
from functools import reduce

__version__ = "2.1.0"
RELEASE_DATE = "2026-11-13"
GITHUB_REPO = "oskare2345/pathfinder-can-bridge"

# ------------------------------------------------------------------ truck facts
DIAG_REQ, DIAG_RSP = 0x780, 0x790          # request 0x780+n, answer 0x790+n
# names as found on the E20 (386-02); other truck types may use the same numbers differently
NODE_NAMES = {
    1: "traction", 2: "lift", 4: "display", 5: "on-board charger",
    6: "access / telematics", 0xA: "LAC (0x78A)", 0xB: "LAC (0x78B)", 0xD: "Li-ion battery",
}
# cyclic CANopen traffic of the E20 - hidden unless "show bus traffic" is on
PERIODIC_IDS = {0x080, 0x0FF, 0x181, 0x182, 0x184, 0x186, 0x213, 0x21E, 0x281, 0x282,
                0x304, 0x313, 0x404, 0x413, 0x481, 0x486, 0x513}
DEFAULTS = dict(interface="slcan", can_port="COM6", adapter_baud=115200, bitrate=500000,
                pf_port="COM8", pf_baud=115200)


def node_name(n):
    return NODE_NAMES.get(n, f"node 0x{n:X}")


# ------------------------------------------------------------------ small helpers
STX = 0x02
DATA_LEN = 8


def xor(bs):
    return reduce(lambda a, b: a ^ b, bs, 0)


def hexs(bs):
    return " ".join(f"{b:02X}" for b in bs)


def parse_hex(text):
    t = text.replace(",", " ").replace("0x", "").replace("0X", "")
    return bytes.fromhex("".join(t.split()))


def stamp():
    return time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"


# ------------------------------------------------------------------ Pathfinder serial codec
@dataclass
class PfFrame:
    cmd: int
    data: bytes

    @property
    def node(self):
        return self.cmd & 0x0F


def _escape(bs):
    out = bytearray()
    for b in bs:
        out += b"\x02\x02" if b == STX else bytes([b])
    return bytes(out)


def encode_frame(data, cmd):
    """CAN data (padded to 8 bytes) -> Pathfinder wire bytes."""
    data = bytes(data).ljust(DATA_LEN, b"\xFF")[:DATA_LEN]
    body = _escape(bytes([cmd, len(data) - 1]) + data)
    return bytes([STX]) + body + _escape(bytes([xor(body)]))


class StreamParser:
    """Feed serial bytes in any chunking; yields (wire_bytes, PfFrame). Bad bytes collect in .junk."""

    def __init__(self):
        self.buf = bytearray()
        self.junk = bytearray()

    def _skip(self, n):
        self.junk += self.buf[:n]
        del self.buf[:n]

    def _try(self):
        b, i, vals, wire, need = self.buf, 1, [], [], None
        while True:
            if i >= len(b):
                return "need"
            if b[i] == STX:
                if i + 1 >= len(b):
                    return "need"
                if b[i + 1] != STX:
                    return None                    # lone 02 = next frame starts -> this one is broken
                vals.append(STX)
                wire += [STX, STX]
                i += 2
            else:
                vals.append(b[i])
                wire.append(b[i])
                i += 1
            if len(vals) == 2:
                if vals[1] > 15:
                    return None
                need = 2 + vals[1] + 1 + 1         # cmd, len, data, checksum
            if need and len(vals) == need:
                cs_len = 2 if vals[-1] == STX else 1
                if xor(wire[:-cs_len]) != vals[-1]:
                    return None
                return i, PfFrame(cmd=vals[0], data=bytes(vals[2:-1]))

    def feed(self, chunk):
        self.buf += chunk
        while True:
            i = self.buf.find(bytes([STX]))
            if i < 0:
                self._skip(len(self.buf))
                return
            if i:
                self._skip(i)
            r = self._try()
            if r == "need":
                return
            if r is None:
                self._skip(1)
                continue
            n, f = r
            raw = bytes(self.buf[:n])
            del self.buf[:n]
            yield raw, f


def decode_frames(raw):
    return list(StreamParser().feed(bytes(raw)))


# ------------------------------------------------------------------ ISO-TP / KWP2000 / CANopen text
KWP_SID = {
    0x10: "StartDiagnosticSession", 0x11: "ECUReset", 0x13: "ReadDiagnosticTroubleCodes",
    0x14: "ClearDiagnosticInformation", 0x17: "ReadStatusOfDTC", 0x18: "ReadDTCByStatus",
    0x1A: "ReadECUIdentification", 0x20: "StopDiagnosticSession", 0x21: "ReadDataByLocalId",
    0x22: "ReadDataByCommonId", 0x23: "ReadMemoryByAddress", 0x27: "SecurityAccess",
    0x2E: "WriteDataByCommonId", 0x2F: "IOControlByCommonId", 0x30: "IOControlByLocalId",
    0x31: "StartRoutine", 0x32: "StopRoutine", 0x33: "RequestRoutineResults",
    0x34: "RequestDownload", 0x35: "RequestUpload", 0x36: "TransferData", 0x37: "RequestTransferExit",
    0x3B: "WriteDataByLocalId", 0x3D: "WriteMemoryByAddress", 0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}
KWP_NRC = {
    0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "notSupported/invalidFormat",
    0x21: "busyRepeatRequest", 0x22: "conditionsNotCorrect", 0x31: "requestOutOfRange",
    0x33: "securityAccessDenied", 0x35: "invalidKey", 0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired", 0x78: "responsePending", 0x80: "notSupportedInActiveSession",
}
WRITE_SIDS = {0x2E, 0x3B, 0x3D, 0x14, 0x11, 0x27, 0x34, 0x36}


def explain_kwp(p):
    if not p:
        return ""
    sid = p[0]
    if sid == 0x7F and len(p) >= 3:
        return f"NEGATIVE {KWP_SID.get(p[1], hex(p[1]))}: {KWP_NRC.get(p[2], hex(p[2]))}"
    if sid >= 0x40 and (sid - 0x40) in KWP_SID:
        return f"OK {KWP_SID[sid - 0x40]} {hexs(p[1:])}".rstrip()
    return f"{KWP_SID.get(sid, f'service 0x{sid:02X}')} {hexs(p[1:])}".rstrip()


def explain_can(d):
    if not d:
        return ""
    pci = d[0] >> 4
    if pci == 0 and 1 <= (d[0] & 0xF) <= 7:
        return explain_kwp(d[1:1 + (d[0] & 0xF)])
    if pci == 1 and len(d) >= 3:
        n = (d[0] & 0xF) << 8 | d[1]
        return f"[{n} bytes, part 1] " + explain_kwp(d[2:])
    if pci == 2:
        return f"[... continued, #{d[0] & 0xF}]"
    if pci == 3:
        return "[flow control]"
    return ""


def is_periodic(msg):
    cid = msg.arbitration_id
    return cid in PERIODIC_IDS or (0x700 < cid < 0x780 and len(msg.data) == 1)


def explain_bus(cid, d):
    if cid == 0x000 and len(d) >= 2:
        cmd = {1: "START", 2: "STOP", 0x80: "PRE-OP", 0x81: "RESET", 0x82: "RESET COMM"}.get(d[0], hex(d[0]))
        return f"CANopen NMT {cmd} {'all nodes' if d[1] == 0 else f'node {d[1]}'}"
    if (0x580 < cid < 0x600 or 0x600 < cid < 0x680) and len(d) == 8:
        node = cid & 0x7F
        return f"CANopen SDO node 0x{node:02X} {'request' if cid > 0x600 else 'answer'}"
    return explain_can(d)


# ------------------------------------------------------------------ fault memory
# Requests Pathfinder uses to read fault lists on the E20 (all read-only):
#   13 01 -> 53 <count> <code LSB-first, 2 bytes> ...      traction, lift, LAC
#   13 03 -> 53 <count> ...                                (second list, usually empty)
#   17 03 -> 57 <count> <records of 16 bytes: code LSB-first + 14 bytes details>   display
# Standard KWP 18 (-> 58 <count> <code MSB-first> <status>) is understood too, for other trucks.
FAULT_REQUESTS = (b"\x13\x01", b"\x13\x03", b"\x17\x03")
FAULT_ANSWER_SIDS = {0x53, 0x57, 0x58}


def parse_faults(answer):
    """Fault answer -> [(code, details_hex)]; [] = no faults; None = layout not understood."""
    if not answer or answer[0] not in FAULT_ANSWER_SIDS or len(answer) < 2:
        return None
    count, body = answer[1], bytes(answer[2:])
    if count == 0:
        return []
    if len(body) % count:
        return None
    size = len(body) // count
    if size < 2:
        return None
    order = "big" if answer[0] == 0x58 else "little"
    return [(int.from_bytes(body[i * size:i * size + 2], order), hexs(body[i * size + 2:(i + 1) * size]))
            for i in range(count)]


class FaultLog:
    """Keeps a history of fault lists in <folder>/faults.csv (opens in Excel).

    Each controller's list is written when it is first seen in a session and whenever it changes.
    faults_state.json remembers the previous list per truck, so codes are marked NEW or CLEARED
    compared with the last time this truck was read - also across days.
    """
    FIELDS = ["date", "time", "truck", "node", "controller", "list", "change", "code_hex", "code_dec",
              "details", "source", "tool_version"]

    def __init__(self, folder, truck=""):
        self.folder = folder or "."
        self.truck = (truck or "").strip() or "unnamed truck"
        self.csv_path = os.path.join(self.folder, "faults.csv")
        self.state_path = os.path.join(self.folder, "faults_state.json")
        self._lock = threading.Lock()
        self._seen = {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}

    def record(self, node, request, answer, source):
        """Store one fault answer. Returns text lines for the screen ([] if unchanged this session)."""
        faults = parse_faults(answer)
        lst = hexs(request) or "?"
        key = f"{self.truck}|{node}|{lst}"
        if faults is None:
            entries, codes = [("?", hexs(answer))], []
        else:
            entries = [(f"{c:04X}", x) for c, x in faults]
            codes = sorted({c for c, _ in entries})
        sig = (tuple(entries))
        with self._lock:
            if self._seen.get(key) == sig:
                return []
            self._seen[key] = sig
            before = self.state.get(key)
            prev = set(before["codes"]) if before else None
            rows, now = [], time.localtime()
            base = dict(date=time.strftime("%Y-%m-%d", now), time=time.strftime("%H:%M:%S", now),
                        truck=self.truck, node=f"0x{node:X}", controller=node_name(node), list=lst,
                        source=source, tool_version=__version__)
            for code, details in entries:
                change = "unparsed" if code == "?" else (
                    "first read" if prev is None else ("NEW" if code not in prev else "still present"))
                rows.append(dict(base, change=change, code_hex=code,
                                 code_dec="" if code == "?" else int(code, 16), details=details))
            gone = sorted(prev - set(codes)) if prev is not None and faults is not None else []
            for code in gone:
                rows.append(dict(base, change="CLEARED", code_hex=code, code_dec=int(code, 16), details=""))
            if not rows:
                rows.append(dict(base, change="no faults", code_hex="", code_dec="", details=""))
            self._write(rows)
            if faults is not None:
                self.state[key] = dict(codes=codes, last_read=time.strftime("%Y-%m-%d %H:%M:%S", now))
                self._save_state()
        new = [c for c, _ in entries if prev is not None and c not in prev and c != "?"]
        head = f"{node_name(node)} list [{lst}]: "
        if faults is None:
            return [head + f"answer not understood, stored raw: [{hexs(answer)}]"]
        text = head + (f"{len(faults)} fault(s): " + " ".join(c for c, _ in entries) if faults else "no faults")
        if new:
            text += f"  | NEW since last read: {' '.join(sorted(set(new)))}"
        if gone:
            text += f"  | CLEARED: {' '.join(gone)}"
        return [text]

    def _write(self, rows):
        # if Excel has faults.csv open (locked), write to a side file instead of losing the rows
        for path in (self.csv_path, os.path.join(self.folder, "faults_while_csv_was_open.csv")):
            try:
                os.makedirs(self.folder, exist_ok=True)
                fresh = not os.path.exists(path)
                with open(path, "a", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, self.FIELDS, delimiter=";")
                    if fresh:
                        w.writeheader()
                    w.writerows(rows)
                return
            except OSError:
                continue

    def _save_state(self):
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            pass


# ------------------------------------------------------------------ friendly errors
class FriendlyError(Exception):
    pass


def explain_port_error(port, e, what):
    s = str(e)
    if "semaphore" in s.lower() or "121" in s:
        hint = "the USB adapter is stuck - unplug it, wait 5 s, plug it back in (check the USB cable)"
    elif "FileNotFound" in s or "could not open port" in s and "Access" not in s:
        hint = f"{port} does not exist - check the COM number in Device Manager / press Refresh"
    elif "Access is denied" in s or "PermissionError" in s or "in use" in s.lower():
        hint = f"{port} is used by another program (old bridge window, terminal, Pathfinder on the wrong port?)"
    else:
        hint = "check the port, the cable and the driver"
    return FriendlyError(f"cannot open {what} on {port}: {s}\n  -> {hint}")


def list_ports():
    """[(device, description)] of all serial ports."""
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    return sorted(((p.device, p.description or "") for p in lp.comports()),
                  key=lambda x: (len(x[0]), x[0]))


def open_can(interface, port, adapter_baud, bitrate):
    """Open the USB-CAN adapter. Returns (can_module, bus)."""
    try:
        import can
    except ImportError:
        raise FriendlyError("python-can is not installed:  pip install python-can")
    if "@" in port:                                     # accept old "COM6@115200" form
        port, b = port.split("@", 1)
        adapter_baud = int(b)
    kw = {"interface": interface, "bitrate": int(bitrate)}
    if interface == "slcan":
        kw["channel"] = f"{port}@{int(adapter_baud)}"
    elif interface == "robotell":
        kw.update(channel=port, ttyBaudrate=int(adapter_baud))
    elif interface == "seeedstudio":
        kw.update(channel=port, baudrate=int(adapter_baud))
    else:
        kw["channel"] = port
    try:
        return can, can.Bus(**kw)
    except Exception as e:  # noqa: BLE001
        raise explain_port_error(port, e, "the CAN adapter")


def open_pf_serial(port, baud):
    try:
        import serial
    except ImportError:
        raise FriendlyError("pyserial is not installed:  pip install pyserial")
    try:
        return serial.Serial(port, int(baud), timeout=0.005)
    except Exception as e:  # noqa: BLE001
        raise explain_port_error(port, e, "the Pathfinder port")


# ------------------------------------------------------------------ the bridge
class Bridge:
    """Pathfinder serial <-> CAN. Runs in its own thread; reports through callbacks.

    on_line(tag, text)  tags: INFO WARN ERROR PF>CAN CAN>PF FC BUS RAW STATUS FAULT
    on_stats(dict)      about once a second
    fault_log           FaultLog: every fault list Pathfinder reads is stored automatically
    quiet_bus           True for trucks that send nothing on their own: forward requests even when
                        the bus is silent (on a truck that is OFF this can lock up slcan adapters)
    """

    def __init__(self, pf_port, can_port, interface="slcan", adapter_baud=115200, bitrate=500000,
                 pf_baud=115200, log_path=None, raw_serial=False, show_bus=False, auto_fc=True,
                 status_every=0.0, on_line=None, on_stats=None, fault_log=None, quiet_bus=False):
        self.cfg = dict(pf_port=pf_port, can_port=can_port, interface=interface, adapter_baud=adapter_baud,
                        bitrate=bitrate, pf_baud=pf_baud)
        self.log_path, self.raw_serial, self.show_bus, self.auto_fc = log_path, raw_serial, show_bus, auto_fc
        self.status_every = status_every
        self.fault_log, self.quiet_bus = fault_log, quiet_bus
        self._asm = {}                 # node -> [total, bytearray] long answer being collected
        self._last_req = {}            # node -> last request payload from Pathfinder
        self.on_line = on_line or (lambda tag, text: print(f"{stamp()}  {tag:<7} {text}", flush=True))
        self.on_stats = on_stats or (lambda s: None)
        self._stop = threading.Event()
        self._thread = None
        self.bus = self.can = self.ser = None
        self.log = None
        self.stats = dict(adapter=False, pf_port=False, fps=0, rx_total=0, requests=0, answers=0,
                          negatives=0, no_answer=0, writes=0, tx_errors=0, last_request=0.0,
                          truck_restarts=0, fault_lists=0, nodes=set())
        self._pending = {}             # node -> (time, payload) of an unanswered request
        self._known_nodes = set()      # controllers Pathfinder has talked to
        self._last_rx = 0.0

    # ---- output
    def emit(self, tag, text):
        if self.log:
            try:
                self.log.write(f"{stamp()}  {tag:<7} {text}\n")
                self.log.flush()
            except OSError:
                pass
        self.on_line(tag, text)

    # ---- life cycle
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bridge", daemon=True)
        self._thread.start()

    def stop(self, wait=3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(wait)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def run_forever(self):
        """Blocking run (command line). Ctrl+C stops."""
        self.start()
        try:
            while self.running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        self.stop()

    def _open(self):
        c = self.cfg
        if not self.bus:
            self.can, self.bus = open_can(c["interface"], c["can_port"], c["adapter_baud"], c["bitrate"])
            self.stats["adapter"] = True
            self.emit("INFO", f"CAN adapter open: {c['interface']} {c['can_port']} "
                              f"(adapter {c['adapter_baud']} baud, bus {c['bitrate']} bit/s)")
        if not self.ser:
            self.ser = open_pf_serial(c["pf_port"], c["pf_baud"])
            self.stats["pf_port"] = True
            self.emit("INFO", f"Pathfinder port open: {c['pf_port']} - set Pathfinder to the OTHER end "
                              f"of the virtual pair")

    def _close(self, which="all"):
        if which in ("all", "can") and self.bus:
            try:
                self.bus.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self.bus = None
            self.stats["adapter"] = False
        if which in ("all", "pf") and self.ser:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.ser = None
            self.stats["pf_port"] = False

    def _run(self):
        if self.log_path:
            try:
                self.log = open(self.log_path, "a", encoding="utf-8")
                self.log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}  START  bridge v{__version__} {self.cfg}\n")
            except OSError as e:
                self.on_line("ERROR", f"cannot open log file {self.log_path}: {e}")
        if self.fault_log:
            self.emit("INFO", f"fault lists read by Pathfinder are saved to {self.fault_log.csv_path} "
                              f"(truck: {self.fault_log.truck})")
        if self.quiet_bus:
            self.emit("WARN", "quiet-bus mode: requests are sent even when the bus is silent")
        sp = StreamParser()
        window, last_stats, last_status, silent_warned = 0, time.time(), time.time(), False
        retry_at = 0.0
        try:
            while not self._stop.is_set():
                # (re)connect
                if not (self.bus and self.ser):
                    if time.time() < retry_at:
                        time.sleep(0.1)
                        continue
                    try:
                        self._open()
                    except FriendlyError as e:
                        self.emit("ERROR", str(e))
                        self.emit("INFO", "retrying in 3 s ...")
                        retry_at = time.time() + 3
                        self._publish_stats(0)
                        continue
                self._where = "pf"           # which side an exception belongs to
                try:
                    # Pathfinder -> CAN
                    chunk = self.ser.read(256)
                    if chunk:
                        if self.raw_serial:
                            self.emit("RAW", f"from Pathfinder: {hexs(chunk)}")
                        for raw, f in sp.feed(chunk):
                            self._from_pathfinder(f)
                        if sp.junk:
                            self.emit("WARN", f"unexpected bytes from Pathfinder: {hexs(sp.junk)}")
                            sp.junk.clear()
                    # CAN -> Pathfinder
                    for _ in range(200):
                        self._where = "can"
                        m = self.bus.recv(0.0)
                        if m is None:
                            break
                        window += 1
                        self._from_can(m)
                except Exception as e:  # noqa: BLE001  - USB adapter unplugged, bad cable, ...
                    which = self._where
                    self.emit("ERROR", f"{'Pathfinder port' if which == 'pf' else 'CAN adapter'} failed: "
                                       f"{type(e).__name__}: {e} - reconnecting (check the USB cable)")
                    self._close(which)
                    retry_at = time.time() + 2
                    continue
                now = time.time()
                self._check_timeouts(now)
                if now - last_stats >= 1.0:
                    fps = window / (now - last_stats)
                    window, last_stats = 0, now
                    self._publish_stats(fps)
                    if fps == 0 and not silent_warned:
                        self.emit("WARN", "no CAN traffic - truck off, wrong bitrate, or adapter not on the bus")
                        silent_warned = True
                    elif fps > 0 and silent_warned:
                        self.emit("INFO", f"CAN traffic OK ({fps:.0f} frames/s)")
                        silent_warned = False
                if self.status_every and now - last_status >= self.status_every:
                    last_status = now
                    s = self.stats
                    self.emit("STATUS", f"bus {s['fps']:.0f} frames/s | requests {s['requests']} "
                                        f"answers {s['answers']} negative {s['negatives']} "
                                        f"no-answer {s['no_answer']} writes {s['writes']}")
        finally:
            self._close()
            self._publish_stats(0)
            self.emit("INFO", "bridge stopped")
            if self.log:
                self.log.close()
                self.log = None

    def _publish_stats(self, fps):
        self.stats["fps"] = fps
        s = dict(self.stats)
        s["nodes"] = sorted(self._known_nodes)
        try:
            self.on_stats(s)
        except Exception:  # noqa: BLE001
            pass

    def _check_timeouts(self, now):
        for n, (t0, payload) in list(self._pending.items()):
            if now - t0 > 2.0:
                del self._pending[n]
                self.stats["no_answer"] += 1
                self.emit("WARN", f"no answer from {node_name(n)} to [{hexs(payload)}] "
                                  f"(truck restarting? controller not fitted?)")

    def _from_pathfinder(self, f):
        if not 0x80 <= f.cmd <= 0x8F:
            self.emit("WARN", f"unknown Pathfinder command 0x{f.cmd:02X} [{hexs(f.data)}] - not forwarded")
            return
        n, d = f.node, f.data
        if self.auto_fc and d and d[0] >> 4 == 3:
            return                                    # we already sent flow control ourselves
        if not self.quiet_bus and time.time() - self._last_rx > 1.0:
            # nobody would ACK it; slcan adapters then retransmit forever and lock up
            self.stats["tx_errors"] += 1
            self.emit("WARN", f"request to {node_name(n)} NOT sent: CAN bus is silent (switch the truck on; "
                              f"if this truck never sends on its own, tick 'Quiet bus')")
            return
        self._known_nodes.add(n)
        self.stats["requests"] += 1
        self.stats["last_request"] = time.time()
        payload = d[1:1 + (d[0] & 0xF)] if d[0] >> 4 == 0 else d
        if d[0] >> 4 == 0:
            self._last_req[n] = bytes(payload)
        if d[0] >> 4 == 0 and payload and payload[0] in WRITE_SIDS:
            self.stats["writes"] += 1
            self.emit("INFO", f"WRITE to {node_name(n)}: [{hexs(payload)}]")
        if d[0] >> 4 in (0, 1):
            self._pending[n] = (time.time(), payload)
        self._where = "can"
        self.bus.send(self.can.Message(arbitration_id=DIAG_REQ + n, data=d, is_extended_id=False), timeout=0.2)
        self._where = "pf"
        self.emit("PF>CAN", f"{node_name(n):<30} [{hexs(d)}]  {explain_can(d)}")

    def _from_can(self, m):
        self._last_rx = time.time()
        self.stats["rx_total"] += 1
        cid, d = m.arbitration_id, bytes(m.data)
        if cid == 0x000 and d[:1] == b"\x01":
            if time.time() - getattr(self, "_last_nmt", 0) > 5:
                self.stats["truck_restarts"] += 1
                self.emit("INFO", "truck controllers (re)started (CANopen NMT start)")
            self._last_nmt = time.time()
        if DIAG_RSP <= cid < DIAG_RSP + 0x10 and (cid - DIAG_RSP) in self._known_nodes:
            n = cid - DIAG_RSP
            if self.auto_fc and d and d[0] >> 4 == 1:
                fc = bytes([0x30, 0, 0]).ljust(DATA_LEN, b"\xFF")
                self.bus.send(self.can.Message(arbitration_id=DIAG_REQ + n, data=fc, is_extended_id=False),
                              timeout=0.2)
                self.emit("FC", f"{node_name(n):<30} flow control sent for long answer")
            pci = d[0] >> 4 if d else 9
            if pci == 0:
                body = d[1:1 + (d[0] & 0xF)]
                if body[:1] == b"\x7f" and len(body) > 2:
                    if body[2] != 0x78:
                        self.stats["negatives"] += 1
                        self._pending.pop(n, None)
                else:
                    self.stats["answers"] += 1
                    self._pending.pop(n, None)
            elif pci == 1:
                self.stats["answers"] += 1
                self._pending.pop(n, None)
            self._where = "pf"
            self.ser.write(encode_frame(d, 0x90 | n))
            self.emit("CAN>PF", f"{node_name(n):<30} [{hexs(d)}]  {explain_can(d)}")
            answer = self._assemble(n, d)
            if answer and answer[0] in FAULT_ANSWER_SIDS and self.fault_log:
                try:
                    for line in self.fault_log.record(n, self._last_req.get(n, b""), answer, "Pathfinder"):
                        self.stats["fault_lists"] += 1
                        self.emit("FAULT", line)
                except Exception as e:  # noqa: BLE001  - fault logging must never stop the bridge
                    self.emit("WARN", f"fault log failed: {type(e).__name__}: {e}")
        elif self.show_bus and not is_periodic(m):
            self.emit("BUS", f"0x{cid:03X} [{hexs(d)}]  {explain_bus(cid, d)}")

    def _assemble(self, n, d):
        """Collect ISO-TP frames from controller n; returns the full answer when complete."""
        pci = d[0] >> 4 if d else 9
        if pci == 0:
            self._asm.pop(n, None)
            return bytes(d[1:1 + (d[0] & 0xF)])
        if pci == 1 and len(d) >= 2:
            self._asm[n] = [((d[0] & 0xF) << 8) | d[1], bytearray(d[2:])]
        elif pci == 2 and n in self._asm:
            total, buf = self._asm[n]
            buf += d[1:]
            if len(buf) >= total:
                del self._asm[n]
                return bytes(buf[:total])
        return None


# ------------------------------------------------------------------ read-only diagnostics
def kwp_request(can, bus, node, payload, timeout=0.6):
    """One KWP request to controller `node`; returns the answer payload or None (handles long answers)."""
    req, rsp = DIAG_REQ + node, DIAG_RSP + node
    while bus.recv(0.0):
        pass
    bus.send(can.Message(arbitration_id=req, is_extended_id=False,
                         data=(bytes([len(payload)]) + bytes(payload)).ljust(DATA_LEN, b"\xFF")))
    end, buf, total = time.time() + timeout, None, 0
    while time.time() < end:
        m = bus.recv(0.05)
        if not m or m.arbitration_id != rsp:
            continue
        d = bytes(m.data)
        pci = d[0] >> 4
        if pci == 0:
            r = d[1:1 + (d[0] & 0x0F)]
            if len(r) >= 3 and r[0] == 0x7F and r[2] == 0x78:
                end = time.time() + 5.0
                continue
            return r
        if pci == 1:
            total, buf = ((d[0] & 0x0F) << 8) | d[1], bytearray(d[2:])
            bus.send(can.Message(arbitration_id=req, is_extended_id=False,
                                 data=bytes([0x30, 0, 0]).ljust(DATA_LEN, b"\xFF")))
            end = time.time() + timeout
        elif pci == 2 and buf is not None:
            buf += d[1:]
            if len(buf) >= total:
                return bytes(buf[:total])
            end = time.time() + timeout
    return None


def wait_for_traffic(bus, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if bus.recv(0.2):
            return True
    return False


def listen(bus, seconds):
    """Count frames per CAN ID for `seconds`. Returns (total, {id: count}, extended_seen)."""
    ids, n, ext, end = {}, 0, False, time.time() + seconds
    while time.time() < end:
        m = bus.recv(0.1)
        if m:
            n += 1
            ext |= bool(m.is_extended_id)
            ids[m.arbitration_id] = ids.get(m.arbitration_id, 0) + 1
    return n, ids, ext


def test_adapter(interface, port, adapter_baud, bitrate, seconds=3.0, out=print, **_):
    """Open the adapter, listen, report. Read-only (the adapter only listens)."""
    can, bus = open_can(interface, port, adapter_baud, bitrate)
    try:
        out(f"adapter opened ({interface} {port}, {adapter_baud} baud, bus {bitrate}) - listening {seconds:.0f} s ...")
        n, ids, ext = listen(bus, seconds)
        if not n:
            out("NO FRAMES. Truck off? Wrong bitrate (E20 = 500000; try 250000 / 125000 on other trucks)? "
                "CAN-H/CAN-L swapped or not connected? Some older trucks only answer when asked - "
                "then try 'Find controllers' with 'Quiet bus' ticked.")
            return False
        top = ", ".join(f"0x{i:03X}" for i, _ in sorted(ids.items(), key=lambda x: -x[1])[:8])
        out(f"OK: {n / seconds:.0f} frames/s from {len(ids)} IDs (busiest: {top})")
        if ext:
            out("note: 29-bit (extended) CAN IDs seen - typical for engine computers (J1939)")
        return True
    finally:
        bus.shutdown()


def _start_tool(can, bus, out, quiet_bus):
    if wait_for_traffic(bus, 5):
        return True
    if quiet_bus:
        out("no CAN traffic in 5 s - 'Quiet bus' is on, asking anyway ...")
        return True
    out("no CAN traffic in 5 s - switch the truck on and try again "
        "(if this truck never sends on its own, tick 'Quiet bus')")
    return False


def _identify(can, bus, n):
    """Session + version + name of controller n, or None if it does not answer. Read-only."""
    r = kwp_request(can, bus, n, b"\x10\x81", 0.25)
    if not r:
        return None
    ver = kwp_request(can, bus, n, b"\x1A\x87", 0.4)
    name = kwp_request(can, bus, n, b"\x1A\x85", 0.4)
    v = f"{int.from_bytes(ver[1:3], 'little') / 1000:.3f}" if ver and ver[0] == 0x5A and len(ver) >= 3 else "?"
    t = name[1:].decode("latin-1", "replace").strip("\x00 ") if name and name[0] == 0x5A else ""
    return dict(node=n, session=hexs(r), session_ok=r[0] == 0x50, version=v, name=t)


def find_controllers(interface, port, adapter_baud, bitrate, out=print, quiet_bus=False, **_):
    """Ask nodes 1..15 for a session + identification. Read-only."""
    can, bus = open_can(interface, port, adapter_baud, bitrate)
    found = []
    try:
        if not _start_tool(can, bus, out, quiet_bus):
            return found
        out("asking controllers 1..15 (read-only) ...")
        for n in range(1, 16):
            c = _identify(can, bus, n)
            if not c:
                continue
            found.append(c)
            extra = "" if c["session_ok"] else f"  (session answer {c['session']})"
            out(f"  node {n:2d} (0x{DIAG_REQ + n:03X}/0x{DIAG_RSP + n:03X})  {node_name(n):<22} "
                f"version {c['version']:<7} {c['name']}{extra}")
        out(f"{len(found)} controllers answered")
        return found
    finally:
        bus.shutdown()


def _read_fault_lists(can, bus, n):
    """[(request, answer)] for every fault request controller n answers positively. Read-only."""
    got = []
    for req in FAULT_REQUESTS:
        # long timeout: LAC modules send each part of a long answer ~2.5 s apart
        ans = kwp_request(can, bus, n, req, 3.0)
        if ans and ans[0] == req[0] + 0x40:
            got.append((req, ans))
    return got


def read_faults(interface, port, adapter_baud, bitrate, out=print, fault_log=None, quiet_bus=False, **_):
    """Read the fault memory of every controller (nothing is cleared or changed). Read-only."""
    can, bus = open_can(interface, port, adapter_baud, bitrate)
    total = 0
    try:
        if not _start_tool(can, bus, out, quiet_bus):
            return 0
        out("reading fault memory of controllers 1..15 (read-only, nothing is cleared) ...")
        for n in range(1, 16):
            if not kwp_request(can, bus, n, b"\x10\x81", 0.25):
                continue
            lists = _read_fault_lists(can, bus, n)
            if not lists:
                out(f"  {node_name(n):<22} answers, but none of the known fault requests")
                continue
            for req, ans in lists:
                if fault_log:
                    for line in fault_log.record(n, req, ans, "Read faults"):
                        out("  " + line)
                else:
                    f = parse_faults(ans)
                    out(f"  {node_name(n)} [{hexs(req)}]: " +
                        ("raw " + hexs(ans) if f is None else " ".join(f"{c:04X}" for c, _ in f) or "no faults"))
                total += len(parse_faults(ans) or [])
        out(f"done - {total} fault entries" + (f", saved to {fault_log.csv_path}" if fault_log else ""))
        return total
    finally:
        bus.shutdown()


def compat_report(interface, port, adapter_baud, bitrate, out=print, folder=".", truck="", quiet_bus=False, **_):
    """Read-only check of a truck; writes a Markdown report to paste into a GitHub issue."""
    try:
        import can as can_mod
        can_ver = getattr(can_mod, "__version__", "?")
    except ImportError:
        can_ver = "not installed"
    lines = [f"# Compatibility report", "",
             f"- tool version: {__version__} ({RELEASE_DATE})",
             f"- date: {time.strftime('%Y-%m-%d %H:%M')}",
             f"- truck (as typed by user): {truck or '(not given - please add type, e.g. 386-02 E20, and year)'}",
             f"- Python {platform.python_version()}, python-can {can_ver}, {platform.system()} {platform.release()}",
             f"- adapter: {interface}, adapter speed {adapter_baud}, CAN bitrate {bitrate}",
             f"- quiet-bus mode: {'on' if quiet_bus else 'off'}", ""]
    can, bus = open_can(interface, port, adapter_baud, bitrate)
    try:
        out("listening 3 s ...")
        n, ids, ext = listen(bus, 3.0)
        lines += ["## Bus traffic (3 s)", ""]
        if n:
            lines.append(f"{n / 3:.0f} frames/s from {len(ids)} IDs" + (", 29-bit IDs present" if ext else ""))
            lines += ["", "| CAN ID | frames/s |", "|---|---|"]
            lines += [f"| 0x{i:03X} | {c / 3:.0f} |" for i, c in sorted(ids.items())[:60]]
        else:
            lines.append("no frames")
        out(f"bus: {n / 3:.0f} frames/s, {len(ids)} IDs")
        lines += ["", "## Controllers (KWP2000 on 0x780+n / 0x790+n)", ""]
        found = []
        if n or quiet_bus:
            lines += ["| node | request/answer ID | session answer | version | name | fault requests answered |",
                      "|---|---|---|---|---|---|"]
            for node in range(1, 16):
                c = _identify(can, bus, node)
                if not c:
                    continue
                lists = _read_fault_lists(can, bus, node)
                fl = ", ".join(f"{hexs(r)} ({len(parse_faults(a) or [])})" for r, a in lists) or "none"
                found.append(c)
                lines.append(f"| {node} | 0x{DIAG_REQ + node:03X}/0x{DIAG_RSP + node:03X} | {c['session']} | "
                             f"{c['version']} | {c['name']} | {fl} |")
                out(f"  node {node}: version {c['version']} {c['name']}  fault requests: {fl}")
        else:
            lines.append("skipped - bus silent (try another bitrate, or tick 'Quiet bus')")
        verdict = ("controllers answer - the bridge should work with this truck" if found else
                   "no controller answered on 0x780+n - this truck needs changes (please post this report)")
        lines += ["", f"**Result:** {verdict}", "",
                  "Before posting: remove anything you do not want to share (e.g. serial numbers)."]
        out(verdict)
    finally:
        bus.shutdown()
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", truck)[:30].strip("_")
    path = os.path.join(folder or ".", f"compat_report_{safe + '_' if safe else ''}{time.strftime('%Y-%m-%d_%H%M')}.md")
    os.makedirs(folder or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    out(f"report saved: {path}")
    return path


# ------------------------------------------------------------------ sharing logs with the developer
# "Send to developer": make an anonymised copy of a log / report / faults.csv and open a pre-filled
# GitHub issue in the browser. Nothing is uploaded by this program - the user attaches the file.
_LOG_DATA = re.compile(r"^(?P<pre>\S+\s+(?P<dir>CAN>PF|PF>CAN)\s+(?P<node>.*?)\s+)"
                       r"\[(?P<hex>[0-9A-F]{2}(?: [0-9A-F]{2})*)\](?P<post>.*)$")
_TEXT_RULES = [
    (re.compile(r"(?i)\b([A-Z]:\\Users\\)[^\\\s/]+"), r"\1<user>", "Windows user names in paths"),
    (re.compile(r"(?i)\b(/home/|/Users/)[^/\s]+"), r"\1<user>", "user names in paths"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>", "e-mail addresses"),
    (re.compile(r"\b[A-Z][0-9A-Z]{2}\d{3}[A-Z]\d{5}\b"), "<truck serial>", "truck serial numbers"),
    (re.compile(r"(?<![\d.])\d{10,}(?![\d.])"), "<number>", "long numbers (IMEI, SIM, serials)"),
]
PRIVATE_NODE_WORDS = ("telematics",)   # E20 node 6: long answers can hold SIM / IMEI / customer data


def _sensitive_positions(payload, private):
    """Byte positions in a complete answer that may identify a truck, SIM or person."""
    bad = set()
    if private and len(payload) > 6 and payload[0] != 0x7F:
        bad.update(range(2, len(payload)))
    run = []
    for i, b in enumerate(list(payload) + [0]):
        if 0x20 <= b <= 0x7E:
            run.append(i)
            continue
        text = bytes(payload[j] for j in run)
        if len(run) >= 8 and sum(c in b"0123456789" for c in text) >= 5:
            bad.update(run)                 # serial-number-like text, e.g. H2X123A45678
        run = []
    return bad


def anonymize_text(text):
    """Returns (clean_text, {what_was_removed: count}). Works on bridge logs, reports and faults.csv."""
    lines = text.splitlines()
    found = {}
    asm, redact = {}, {}                  # (dir, node) -> [total, [(line, pos)...], bytes]; line -> {pos}

    for li, ln in enumerate(lines):
        if re.match(r"^\S+\s+RAW\s", ln):
            lines[li] = re.sub(r"(RAW\s+).*", r"\1(raw bytes removed)", ln)
            continue
        m = _LOG_DATA.match(ln)
        if not m:
            continue
        d = bytes.fromhex(m["hex"].replace(" ", ""))
        name = m["node"].strip()
        private = any(w in name for w in PRIVATE_NODE_WORDS)
        key = (m["dir"], name)
        pci = d[0] >> 4 if d else 9
        done = None
        if pci == 0:
            n = d[0] & 0xF
            done = (d[1:1 + n], [(li, 1 + i) for i in range(n)])
        elif pci == 1 and len(d) >= 2:
            asm[key] = [((d[0] & 0xF) << 8) | d[1], [(li, 2 + i) for i in range(len(d) - 2)], bytearray(d[2:])]
        elif pci == 2 and key in asm:
            a = asm[key]
            a[1] += [(li, 1 + i) for i in range(len(d) - 1)]
            a[2] += d[1:]
            if len(a[2]) >= a[0]:
                done = (bytes(a[2][:a[0]]), a[1][:a[0]])
                del asm[key]
        if done:
            for p in _sensitive_positions(done[0], private):
                l2, pos = done[1][p]
                redact.setdefault(l2, set()).add(pos)

    for li, pos in redact.items():
        m = _LOG_DATA.match(lines[li])
        bs = m["hex"].split()
        for p in pos:
            if p < len(bs):
                bs[p] = "**"
        lines[li] = f"{m['pre']}[{' '.join(bs)}]  (data removed for privacy)"
    if redact:
        found["identifying bytes in controller answers"] = sum(len(p) for p in redact.values())

    out = "\n".join(lines)
    for rx, repl, what in _TEXT_RULES:
        out, n = rx.subn(repl, out)
        if n:
            found[what] = found.get(what, 0) + n
    return out + ("\n" if text.endswith("\n") else ""), found


def make_share_copy(path, out_dir=None):
    """Anonymised copy next to the original (or in out_dir). Returns (new_path, found)."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        clean, found = anonymize_text(f.read())
    base, ext = os.path.splitext(os.path.basename(path))
    ext = ext if ext.lower() in (".log", ".md", ".csv", ".txt") else ".txt"
    new = os.path.join(out_dir or os.path.dirname(os.path.abspath(path)),
                       f"SHARE_{base}_{time.strftime('%H%M%S')}{ext}")
    header = "" if ext in (".csv", ".md") else (
        f"# anonymised by Pathfinder CAN bridge v{__version__} - check before sharing\n")
    with open(new, "w", encoding="utf-8") as f:
        f.write(header + clean)
    return new, found


def problem_summary(text, limit=12):
    """Last ERROR / WARN / NEGATIVE lines of an (anonymised) log, for the issue body."""
    hits = [ln for ln in text.splitlines()
            if re.search(r"\s(ERROR|WARN)\s|NEGATIVE", ln) and "(data removed" not in ln]
    return hits[-limit:]


def issue_url(kind, title, body, repo=GITHUB_REPO):
    """Pre-filled 'new issue' page. kind: 'bug' or 'compatibility'."""
    from urllib.parse import urlencode
    template = "compatibility_report.md" if kind == "compatibility" else "bug_report.md"
    labels = "compatibility" if kind == "compatibility" else "bug"
    body = body if len(body) < 5500 else body[:5500] + "\n... (cut - see attached file)"
    q = urlencode(dict(template=template, labels=labels, title=title, body=body))
    return f"https://github.com/{repo}/issues/new?{q}"


def issue_body(kind, what, when="", truck="", adapter="", summary=(), attached=""):
    lines = [f"**What happened:** {what or '(please describe)'}"]
    if when:
        lines.append(f"**When (time in the log):** {when}")
    lines += [f"**Truck:** {truck or '(type number from the data plate)'}",
              f"**Adapter:** {adapter or '?'}",
              f"**Tool version:** {__version__} ({RELEASE_DATE}), {platform.system()} {platform.release()}, "
              f"Python {platform.python_version()}", "",
              f"**Attached file:** `{attached}` - drag it into this box from the folder that just opened"
              if attached else "**Attached file:** (none)"]
    if summary:
        lines += ["", "<details><summary>Last problems in the log</summary>", "", "```"] + list(summary) + \
                 ["```", "</details>"]
    lines += ["", "<!-- created by the 'Send to developer' button. Check the attached file before posting. -->"]
    return "\n".join(lines)


SAFE_SCAN = {0x1A: "ReadECUIdentification", 0x21: "ReadDataByLocalId"}


def cmd_diag(a):
    reqs = [parse_hex(r) for r in a.requests]
    for p in reqs:
        if p and p[0] in WRITE_SIDS:
            sys.exit(f"'{hexs(p)}' would write/change something - diag is read-only")
    can, bus = open_can(a.interface, a.can_port, a.adapter_baud, a.bitrate)
    try:
        if not wait_for_traffic(bus, 30):
            sys.exit("no CAN traffic - truck off?")
        for p in reqs:
            ans = kwp_request(can, bus, a.node, p)
            print(f"{node_name(a.node)} [{hexs(p)}] -> {'NO ANSWER' if ans is None else hexs(ans)}  "
                  f"{explain_kwp(ans) if ans else ''}")
        if a.scan:
            svc = int(a.scan, 16)
            if svc not in SAFE_SCAN:
                sys.exit("--scan allows only 21 or 1A (read-only)")
            kwp_request(can, bus, a.node, b"\x10\x81")
            for i in range(256):
                ans = kwp_request(can, bus, a.node, bytes([svc, i]), 0.3)
                if ans and ans[0] == svc + 0x40:
                    val = ans[2:] if svc == 0x21 else ans[1:]
                    print(f"  {svc:02X} {i:02X} = [{hexs(val)}]  {val.decode('latin-1', 'replace')!r}")
    finally:
        bus.shutdown()


# ------------------------------------------------------------------ command line
def add_adapter_args(p):
    p.add_argument("-i", "--interface", default=DEFAULTS["interface"], help="slcan (default), seeedstudio, robotell")
    p.add_argument("-c", "--can-port", default=DEFAULTS["can_port"], help="CAN adapter COM port (default COM6)")
    p.add_argument("--adapter-baud", type=int, default=DEFAULTS["adapter_baud"],
                   help="serial speed of the CAN adapter (default 115200)")
    p.add_argument("-b", "--bitrate", type=int, default=DEFAULTS["bitrate"], help="truck CAN bitrate (default 500000)")
    p.add_argument("--quiet-bus", action="store_true",
                   help="truck sends nothing on its own: send requests even when the bus is silent")


def add_truck_args(p):
    p.add_argument("--truck", default="", help='name for this truck in faults.csv / reports, e.g. "E20 386-02"')
    p.add_argument("--folder", default=".", help="where faults.csv / reports are written (default: current folder)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"Pathfinder CAN bridge v{__version__} (unofficial, for Linde trucks)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} ({RELEASE_DATE})")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("gui", help="open the window (default when no command is given)")
    sub.add_parser("ports", help="list serial ports")

    p = sub.add_parser("bridge", help="run the bridge in this console")
    add_adapter_args(p)
    p.add_argument("-p", "--pf-port", "--serial", default=DEFAULTS["pf_port"],
                   help="bridge end of the virtual serial pair (default COM8)")
    p.add_argument("--log", help="also write everything to this file")
    p.add_argument("--raw-serial", action="store_true", help="show every byte from Pathfinder")
    p.add_argument("--show-bus", action="store_true", help="show non-cyclic CAN traffic too")
    p.add_argument("--no-auto-fc", action="store_true", help="do not send ISO-TP flow control")
    p.add_argument("--status", type=float, default=10, help="seconds between status lines (0 = off)")
    p.add_argument("--no-fault-log", action="store_true", help="do not save fault lists to faults.csv")
    add_truck_args(p)
    p.add_argument("--passthrough", action="store_true", help=argparse.SUPPRESS)   # old flag, always on now

    p = sub.add_parser("test", help="check the CAN adapter: open it and count truck frames (read-only)")
    add_adapter_args(p)
    p = sub.add_parser("nodes", help="list the truck's controllers and their versions (read-only)")
    add_adapter_args(p)
    p = sub.add_parser("faults", help="read all fault memories and add them to faults.csv (read-only)")
    add_adapter_args(p)
    add_truck_args(p)
    p = sub.add_parser("report", help="compatibility report for a new truck type -> .md file (read-only)")
    add_adapter_args(p)
    add_truck_args(p)
    p = sub.add_parser("diag", help='read-only KWP requests, e.g. diag --node 1 "10 81" "1A 87" [--scan 21]')
    add_adapter_args(p)
    p.add_argument("requests", nargs="*", default=["10 81"])
    p.add_argument("--node", type=lambda s: int(s, 0), default=1, help="controller (1 traction, 2 lift, 4 display ...)")
    p.add_argument("--scan", help="try all 256 IDs of service 21 or 1A")
    p = sub.add_parser("decode", help='explain Pathfinder wire bytes, e.g. decode "02 81 07 02 02 10 81 FF FF FF FF FF E8"')
    p.add_argument("hex")
    p = sub.add_parser("share", help="anonymised copy of a log/report + link to a pre-filled GitHub issue")
    p.add_argument("file", help="bridge_YYYY-MM-DD.log, compat_report_*.md or faults.csv")
    p.add_argument("--what", default="", help="what happened")
    p.add_argument("--when", default="", help="time of the problem, e.g. 16:46")
    p.add_argument("--truck", default="")
    p.add_argument("--open", action="store_true", help="open the issue page in the browser")

    a = ap.parse_args(argv)
    try:
        if a.cmd in (None, "gui"):
            import pathfinder_gui
            pathfinder_gui.main()
        elif a.cmd == "ports":
            for dev, desc in list_ports():
                print(f"{dev:<8} {desc}")
        elif a.cmd == "bridge":
            Bridge(a.pf_port, a.can_port, a.interface, a.adapter_baud, a.bitrate, log_path=a.log,
                   raw_serial=a.raw_serial, show_bus=a.show_bus, auto_fc=not a.no_auto_fc,
                   status_every=a.status, quiet_bus=a.quiet_bus,
                   fault_log=None if a.no_fault_log else FaultLog(a.folder, a.truck)).run_forever()
        elif a.cmd == "test":
            test_adapter(a.interface, a.can_port, a.adapter_baud, a.bitrate)
        elif a.cmd == "nodes":
            find_controllers(a.interface, a.can_port, a.adapter_baud, a.bitrate, quiet_bus=a.quiet_bus)
        elif a.cmd == "faults":
            read_faults(a.interface, a.can_port, a.adapter_baud, a.bitrate, quiet_bus=a.quiet_bus,
                        fault_log=FaultLog(a.folder, a.truck))
        elif a.cmd == "report":
            compat_report(a.interface, a.can_port, a.adapter_baud, a.bitrate, quiet_bus=a.quiet_bus,
                          folder=a.folder, truck=a.truck)
        elif a.cmd == "diag":
            cmd_diag(a)
        elif a.cmd == "share":
            new, found = make_share_copy(a.file)
            print(f"anonymised copy: {new}")
            for what, n in found.items():
                print(f"  removed {n} x {what}")
            kind = "compatibility" if os.path.basename(a.file).startswith("compat_report") else "bug"
            with open(new, encoding="utf-8") as f:
                summ = problem_summary(f.read())
            url = issue_url(kind, f"[{'truck' if kind == 'compatibility' else 'bug'}] {a.truck or a.what[:60]}",
                            issue_body(kind, a.what, a.when, a.truck, summary=summ,
                                       attached=os.path.basename(new)))
            print("CHECK THE COPY, then attach it to the issue:\n" + url)
            if a.open:
                import webbrowser
                webbrowser.open(url)
        elif a.cmd == "decode":
            frames = decode_frames(parse_hex(a.hex))
            if not frames:
                sys.exit("no valid Pathfinder frame found (check the bytes / checksum)")
            for raw, f in frames:
                kind = "request to" if f.cmd < 0x90 else "answer from"
                print(f"{hexs(raw)}\n  cmd 0x{f.cmd:02X} = {kind} {node_name(f.node)} "
                      f"(CAN 0x{(DIAG_REQ if f.cmd < 0x90 else DIAG_RSP) + f.node:03X})\n"
                      f"  data [{hexs(f.data)}]  {explain_can(f.data)}")
    except FriendlyError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
