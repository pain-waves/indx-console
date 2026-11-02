# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""USB serial link to a Prusa CORE One INDX.

One worker thread owns the serial port. It runs queued actions (load, unload,
set material) first, and otherwise polls the printer:
  M105  every few seconds   -> per-tool nozzle temps, bed, chamber
  M27   every few seconds   -> printing or idle, progress
  M865 I<n> for every tool  -> loaded filament name (nothing printed = empty)

Every line the printer sends is checked for temperature data, whichever command
it arrived with, and kept in a short raw log for the console's diagnostics view.
"""
from __future__ import annotations

import collections
import copy
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("indx.printer")

PRUSA_VID = 0x2C99
TOOL_COUNT = 8

POLL_TEMPS_S = 3.0
POLL_JOB_S = 5.0
POLL_FILAMENT_S = 30.0
CMD_TIMEOUT_S = 8.0
LONG_CMD_TIMEOUT_S = 20 * 60.0  # load/unload wait for the user at the printer

MATERIALS = ["PLA", "PETG", "ASA", "PC", "PVB", "ABS", "HIPS", "PP", "FLEX", "PA", "PPS", "PPA"]

_NUM = r"(-?\d+(?:\.\d+)?)"
_RE_TOOL_TEMP = re.compile(r"(?<![\w@])T(\d):\s*" + _NUM + r"(?:\s*/\s*" + _NUM + r")?")
_RE_BED = re.compile(r"(?<![\w@])B:\s*" + _NUM + r"(?:\s*/\s*" + _NUM + r")?")
_RE_CHAMBER = re.compile(r"(?<![\w])C@?:\s*" + _NUM)
_RE_M73 = re.compile(r"M73 Progress:\s*(\d+)%;?(?:\s*Time left:\s*(\d+)m)?")
_RE_IPV4 = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})$")
_RE_M31 = re.compile(r"Print time:\s*((?:\d+[ydhms]\s*)+)")
_RE_SD_BYTE = re.compile(r"SD printing byte\s+(\d+)\s*/\s*(\d+)")
_RE_MACHINE = re.compile(r"MACHINE_TYPE:(\S+)")
_RE_FW = re.compile(r"FIRMWARE_NAME:(.+?)\s+SOURCE_CODE_URL")
_RE_MATERIAL = re.compile(r"^[A-Za-z0-9 _+\-.]{1,20}$")


@dataclass
class ToolState:
    index: int
    material: str | None = None          # None = empty, as reported by M865
    nozzle_temp: float | None = None
    nozzle_target: float | None = None
    activity: str | None = None          # "loading" | "unloading" | "setting" | None
    activity_note: str | None = None


@dataclass
class PrinterState:
    connected: bool = False
    port: str | None = None
    machine: str | None = None
    firmware: str | None = None
    printing: bool = False
    progress: float | None = None        # 0-100 while printing
    file_name: str | None = None         # short (8.3) name from M27 C
    time_left_min: int | None = None     # from the print's M73 lines
    print_started: float | None = None   # when the print started (from M31, so right even mid-print)
    ip_address: str | None = None        # printer's network address, from M46
    bed_temp: float | None = None
    bed_target: float | None = None
    chamber_temp: float | None = None
    busy: bool = False                   # a load/unload is running
    waiting_for_user: bool = False       # printer reported "paused for user"
    last_error: str | None = None
    last_update: float = 0.0
    filament_checked: bool = False       # M865 has run for every tool at least once
    tools: list[ToolState] = field(default_factory=lambda: [ToolState(i) for i in range(TOOL_COUNT)])


class CommandError(Exception):
    pass


def check_material(material: str | None) -> str:
    material = (material or "").strip()
    if not material:
        raise ValueError("Choose a material first")
    if not _RE_MATERIAL.match(material):
        raise ValueError("Material names may use letters, numbers, spaces and - _ + .")
    return material


class Printer:
    """Owns the serial port and keeps PrinterState current."""

    def __init__(self, port: str | None = None, transport_factory=None, on_materials=None, on_ip=None):
        self._on_ip = on_ip                          # callback(str) when M46 reports the printer's address
        self._wanted_port = port
        self._transport_factory = transport_factory  # for the simulator
        self._on_materials = on_materials            # callback(list[str|None]) after each full M865 sweep
        self._ser = None
        self.state = PrinterState()
        self._lock = threading.Lock()                 # guards self.state
        self._actions: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="printer", daemon=True)
        self._next_temps = self._next_job = self._next_fil = 0.0
        self.raw_log: collections.deque = collections.deque(maxlen=300)

    # ---------- public ----------
    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def snapshot(self) -> PrinterState:
        with self._lock:
            return copy.deepcopy(self.state)

    def can_act(self) -> tuple[bool, str | None]:
        s = self.state
        if not s.connected:
            return False, "Printer not connected over USB"
        if s.printing:
            return False, "A print is running"
        if s.busy or not self._actions.empty():
            return False, "Another load or unload is in progress"
        return True, None

    def request_load(self, tool: int, material: str):
        self._enqueue("load", tool, check_material(material))

    def request_unload(self, tool: int):
        self._enqueue("unload", tool, None)

    def request_set_material(self, tool: int, material: str):
        self._enqueue("set", tool, check_material(material))

    def request_raw(self, line: str, timeout: float = CMD_TIMEOUT_S):
        """Send one command that needs no tool, e.g. printer setup. Its replies go to self.raw_result."""
        if not self.state.connected:
            raise CommandError("Printer not connected over USB")
        if self.state.busy:
            raise CommandError("The printer is busy")
        self._actions.put(("raw", None, (line, timeout)))

    raw_result: list[str] | None = None

    def request_refresh(self):
        self._next_fil = self._next_temps = self._next_job = 0.0

    def raw_lines(self, n: int = 80) -> list[str]:
        return list(self.raw_log)[-n:]

    # ---------- internals ----------
    def _enqueue(self, kind, tool, material):
        if not 0 <= tool < TOOL_COUNT:
            raise ValueError("bad tool index")
        ok, why = self.can_act()
        if not ok:
            raise CommandError(why)
        with self._lock:
            self.state.tools[tool].activity = {"load": "loading", "unload": "unloading", "set": "setting"}[kind]
            self.state.tools[tool].activity_note = "Queued"
        self._actions.put((kind, tool, material))

    def _run(self):
        while not self._stop.is_set():
            if self._ser is None:
                if not self._connect():
                    self._stop.wait(3)
                    continue
            try:
                try:
                    action = self._actions.get_nowait()
                except queue.Empty:
                    action = None
                if action:
                    self._do_action(*action)
                else:
                    self._poll()
                    self._stop.wait(0.2)
            except (OSError, CommandError) as e:
                log.warning("printer link problem: %s", e)
                self._set(last_error=str(e))
                if isinstance(e, OSError):
                    self._disconnect()
            except Exception as e:  # noqa: BLE001 - never let the worker die
                log.exception("unexpected printer error")
                self._set(last_error=f"Unexpected error: {e}")
                self._stop.wait(1)

    def _connect(self) -> bool:
        try:
            if self._transport_factory:
                self._ser, port = self._transport_factory(), "simulator"
            else:
                port = self._wanted_port or find_printer_port()
                if not port:
                    self._set(connected=False, port=None, last_error="No Prusa printer found on USB")
                    return False
                import serial
                self._ser = serial.Serial(port, 115200, timeout=0.2, write_timeout=2)
            time.sleep(0.5)
            self._drain()
            text = "\n".join(self._query("M115"))
            m, fw = _RE_MACHINE.search(text), _RE_FW.search(text)
            self._set(connected=True, port=port, last_error=None,
                      machine=m.group(1) if m else None, firmware=fw.group(1) if fw else None)
            for setup in ("M155 S3",              # printer sends temperatures by itself every 3 s
                          "M331 side_fsensor"):   # side filament sensor metric (resets on printer restart)
                try:
                    self._query(setup)
                except CommandError:
                    pass
            self._read_ip()
            self.request_refresh()
            log.info("connected to %s on %s", self.state.machine, port)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("connect failed: %s", e)
            self._disconnect(error=f"Connect failed: {e}")
            return False

    def _disconnect(self, error: str | None = None):
        try:
            if self._ser:
                self._ser.close()
        except Exception:  # noqa: BLE001
            pass
        self._ser = None
        with self._lock:
            self.state.connected = False
            self.state.busy = False
            self.state.waiting_for_user = False
            self.state.printing = False
            for t in self.state.tools:
                t.activity = t.activity_note = None
            if error:
                self.state.last_error = error

    def _drain(self):
        end = time.time() + 0.3
        while time.time() < end:
            raw = self._ser.readline()
            if not raw:
                break
            self._receive(raw.decode("ascii", "replace").strip())

    def _receive(self, text: str):
        """Every received line passes through here, whatever command it belongs to."""
        if not text:
            return
        self.raw_log.append(f"{time.strftime('%H:%M:%S')} < {text}")
        if "T0:" in text or "B:" in text or "C@:" in text or "T:" in text:
            self._parse_temps(text)
        if "M73 Progress" in text:      # echoed as the print file runs, or asked for with a bare M73
            m = _RE_M73.search(text)
            if m:
                with self._lock:
                    self.state.progress = float(m.group(1))
                    if m.group(2) is not None:
                        self.state.time_left_min = int(m.group(2))
        elif text.lstrip().startswith("Time left:"):   # the bare-M73 reply puts this on its own line
            m = re.search(r"Time left:\s*(\d+)m", text)
            if m:
                with self._lock:
                    self.state.time_left_min = int(m.group(1))

    _seq = 0

    def _query(self, line: str, timeout: float | None = None, fail_markers: tuple[str, ...] = ()) -> list[str]:
        """Send a short command followed by a bookmark (M118 SYNC<n>) and read up to that bookmark.

        While printing, the printer can answer USB commands late. Without a bookmark a late
        reply gets taken as the answer to the next command, and every reply after it is
        matched to the wrong command. With one, each reply is bounded exactly.
        """
        if timeout is None:
            timeout = 20.0 if self.state.printing else CMD_TIMEOUT_S
        self._seq += 1
        marker = f"SYNC{self._seq}"
        self.raw_log.append(f"{time.strftime('%H:%M:%S')} > {line}  (+ M118 {marker})")
        self._ser.write((line + "\n" + f"M118 {marker}\n").encode("ascii"))
        replies: list[str] = []
        failed = None
        deadline = time.time() + timeout
        while True:
            raw = self._ser.readline()
            if not raw:
                if time.time() > deadline:
                    self._resync(marker)
                    raise CommandError(f"No reply to {line.split()[0]}")
                continue
            text = raw.decode("ascii", "replace").strip()
            if not text:
                continue
            self._receive(text)
            if text.endswith(marker):            # bookmark reached; consume its own "ok"
                self._read_until_ok(deadline + 5)
                if failed:
                    raise CommandError(failed)
                return replies
            low = text.lower()
            if any(m in low for m in fail_markers):
                failed = f"{line}: {text}"
                continue
            if low.startswith("echo:busy") or low.startswith("busy:"):
                deadline = time.time() + timeout
                continue
            if text.startswith("ok"):
                rest = text[2:].strip()
                if rest:
                    replies.append(rest)
                continue
            replies.append(text)

    def _read_until_ok(self, deadline: float):
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            text = raw.decode("ascii", "replace").strip()
            self._receive(text)
            if text.startswith("ok"):
                return

    def _resync(self, marker: str, wait_s: float = 60.0):
        """Skip everything until our bookmark comes back, so the next command starts clean."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            text = raw.decode("ascii", "replace").strip()
            self._receive(text)
            if text.endswith(marker):
                self._read_until_ok(time.time() + 5)
                return
        raise OSError("Lost track of the printer's replies; reconnecting")

    def _command(self, line: str, timeout: float = CMD_TIMEOUT_S, on_keepalive=None,
                 fail_markers: tuple[str, ...] = ()) -> list[str]:
        """Send one line and collect replies up to 'ok' (used for long actions like load/unload).

        fail_markers: reply text that means the command ended without an 'ok'
        (raises CommandError straight away instead of waiting for the timeout)."""
        self.raw_log.append(f"{time.strftime('%H:%M:%S')} > {line}")
        self._ser.write((line + "\n").encode("ascii"))
        replies: list[str] = []
        deadline = time.time() + timeout
        while True:
            raw = self._ser.readline()
            if not raw:
                if time.time() > deadline:
                    raise CommandError(f"No reply to {line.split()[0]}")
                continue
            text = raw.decode("ascii", "replace").strip()
            if not text:
                continue
            self._receive(text)
            low = text.lower()
            if any(m in low for m in fail_markers):
                raise CommandError(f"{line}: {text}")
            if low.startswith("echo:busy") or low.startswith("busy:"):
                deadline = time.time() + timeout  # printer is alive, keep waiting
                if on_keepalive:
                    on_keepalive(text)
                continue
            if text.startswith("ok"):
                rest = text[2:].strip()
                if rest:
                    replies.append(rest)
                return replies
            replies.append(text)

    def _poll(self):
        now = time.time()
        if now >= self._next_temps:
            self._next_temps = now + POLL_TEMPS_S
            # Only ask if the automatic reports (M155) have gone quiet.
            if now - self._last_temp_rx > 2 * POLL_TEMPS_S:
                self._read_temps()
        if now >= self._next_job:
            self._next_job = now + POLL_JOB_S
            was_printing = self.state.printing
            self._parse_job(self._query("M27"))
            if not was_printing and self.state.printing:
                self._set(print_started=time.time(), time_left_min=None)
                self._read_print_details()
            elif self.state.printing and now >= self._next_m73:
                self._ask_progress()
            if was_printing and not self.state.printing:
                self._set(file_name=None, time_left_min=None, print_started=None)
                self._next_fil = 0.0          # print just ended: re-read every tool's material
        # Materials can't change mid-print, so they're not re-read then -- except the first
        # read after connecting, which always happens.
        if now >= self._next_fil and (not self.state.printing or not self.state.filament_checked):
            self._next_fil = now + POLL_FILAMENT_S
            for i in range(TOOL_COUNT):
                self._read_material(i)
            self._set(filament_checked=True)
            if self._on_materials:
                self._on_materials([t.material for t in self.snapshot().tools])
        if self._ip_wanted or (now >= self._next_ip and not self.state.printing):
            self._ip_wanted = False
            self._read_ip()
        self._set(last_update=time.time())

    # M105 on the INDX only answers when it can resolve a tool:
    #  - idle, all tools parked: bare "M105" fails, "M105 T0" works
    #  - printing: a tool is picked up so bare "M105" works, but "M105 T<n>" goes through the
    #    print's tool mapping and fails for tool numbers the print doesn't use
    # A failure prints "Invalid extruder" and never sends "ok". Any form that works reports
    # all eight nozzles, the bed and the chamber, so try forms until one answers.
    _TEMP_FORMS = ["M105 T0", "M105"] + [f"M105 T{i}" for i in range(1, 8)]
    _temp_form = "M105 T0"

    def _read_temps(self):
        forms = [self._temp_form] + [f for f in self._TEMP_FORMS if f != self._temp_form]
        if self.state.printing and self._temp_form != "M105":
            forms.remove("M105")
            forms.insert(0, "M105")
        for form in forms[:4]:          # keep each poll short
            try:
                self._query(form, fail_markers=("invalid extruder",))
            except CommandError:
                continue
            self._temp_form = form
            return
        # nothing answered this time; try again next poll without raising an error banner

    _next_m73 = 0.0
    _next_ip = 0.0
    _ip_wanted = False

    def request_ip(self):
        """Ask the printer for its network address on the next poll (works during prints too)."""
        if not self.state.connected:
            raise CommandError("The printer isn't connected over USB")
        self._ip_wanted = True

    def _read_print_details(self):
        """File name (M27 C), how long it's been printing (M31) and time left (M73).
        Asked for directly so the values are right even if the console started mid-print."""
        try:
            for ln in self._query("M27 C"):
                if ln.startswith("Current file:"):
                    self._set(file_name=ln.split(":", 1)[1].strip() or None)
        except CommandError:
            pass
        try:
            text = " ".join(self._query("M31"))
            m = _RE_M31.search(text)
            if m:
                secs = sum(int(n) * {"y": 31536000, "d": 86400, "h": 3600, "m": 60, "s": 1}[u]
                           for n, u in re.findall(r"(\d+)([ydhms])", m.group(1)))
                self._set(print_started=time.time() - secs)
        except CommandError:
            pass
        self._ask_progress()

    def _ask_progress(self):
        """A bare M73 reports progress and time left without changing anything."""
        self._next_m73 = time.time() + 30
        try:
            self._query("M73")          # replies pass through _receive, which parses them
        except CommandError:
            pass

    def _read_ip(self):
        self._next_ip = time.time() + 600
        try:
            for ln in self._query("M46"):
                m = _RE_IPV4.match(ln.strip())
                if m and m.group(1) != "0.0.0.0":
                    if m.group(1) != self.state.ip_address:
                        self._set(ip_address=m.group(1))
                        if self._on_ip:
                            self._on_ip(m.group(1))
        except CommandError:
            pass

    def _read_material(self, tool: int):
        lines = self._query(f"M865 I{tool}")
        name = None
        for ln in lines:
            if ln.startswith("name:"):
                name = ln[5:].strip() or None
        if name == "---":
            name = None
        with self._lock:
            self.state.tools[tool].material = name

    _last_temp_rx = 0.0

    def _parse_temps(self, text: str):
        with self._lock:
            for m in _RE_TOOL_TEMP.finditer(text):
                i = int(m.group(1))
                if 0 <= i < TOOL_COUNT:
                    self.state.tools[i].nozzle_temp = float(m.group(2))
                    if m.group(3) is not None:
                        self.state.tools[i].nozzle_target = float(m.group(3))
            b = _RE_BED.search(text)
            if b:
                self.state.bed_temp = float(b.group(1))
                if b.group(2) is not None:
                    self.state.bed_target = float(b.group(2))
            c = _RE_CHAMBER.search(text)
            if c:
                self.state.chamber_temp = float(c.group(1))
            if b or c:
                self._last_temp_rx = time.time()

    def _parse_job(self, lines: list[str]):
        text = " ".join(lines)
        m = _RE_SD_BYTE.search(text)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if self.state.time_left_min is None:   # M73 progress from the file is more accurate
                self._set(progress=round(100 * done / total, 1) if total else None)
            self._set(printing=True)
        elif "Not SD printing" in text:
            self._set(printing=False, progress=None)

    def _do_action(self, kind: str, tool: int | None, material):
        if kind == "raw":
            line, timeout = material
            self._set(busy=True)
            try:
                self.raw_result = self._command(line, timeout, lambda _t: None)
            finally:
                self._set(busy=False)
            return

        def keepalive(text):
            waiting = "paused for user" in text or "waiting for user" in text
            with self._lock:
                self.state.waiting_for_user = waiting
                self.state.tools[tool].activity_note = "waiting_user" if waiting else "working"

        self._set(busy=True, last_error=None)
        with self._lock:
            self.state.tools[tool].activity_note = "working"
        try:
            if kind == "load":
                out = self._command(f'M701 T{tool} S"{material}"', LONG_CMD_TIMEOUT_S, keepalive)
            elif kind == "unload":
                out = self._command(f"M702 T{tool}", LONG_CMD_TIMEOUT_S, keepalive)
            else:  # set material without moving filament
                out = self._query(f'M865 S"{material}" L{tool}')
            errors = [ln for ln in out if ln.lower().startswith(("error", "echo:filament type invalid"))]
            if errors:
                self._set(last_error=f"Tool {tool + 1}: {errors[0]}")
            self._read_material(tool)
        finally:
            with self._lock:
                self.state.busy = False
                self.state.waiting_for_user = False
                self.state.tools[tool].activity = None
                self.state.tools[tool].activity_note = None
            self._next_temps = 0.0

    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self.state, k, v)


def find_printer_port() -> str | None:
    """Return the serial port of the first Prusa device (USB vendor 0x2C99)."""
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid == PRUSA_VID:
            return p.device
    return None
