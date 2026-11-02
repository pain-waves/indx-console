# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""A fake CORE One INDX on a fake serial port, for testing without the printer.

Run the console with INDX_SIMULATE=1 to use it.
"""
from __future__ import annotations

import collections
import os
import random
import re
import threading
import time

PRESETS = {"PLA": 215, "PETG": 240, "ASA": 260, "PC": 275, "PVB": 215, "ABS": 255,
           "HIPS": 220, "PP": 240, "FLEX": 240, "PA": 285, "PPS": 330, "PPA": 300}


class SimPrinter:
    def __init__(self):
        self.loaded = ["PLA", "PETG", None, "ASA", "PLA", None, "PETG", None]
        self.targets = [0.0] * 8
        self.temps = [24.0] * 8
        self.bed = 24.0
        self.printing = os.environ.get("INDX_SIM_PRINTING") == "1"   # test hook: start mid-print
        self.pos = 0
        self._out: collections.deque[bytes] = collections.deque()
        self._lock = threading.Lock()
        self._job: tuple | None = None   # (kind, tool, material, finish_time)
        self.autoreport = 0
        self._next_report = 0.0

    # serial-like API
    def write(self, data: bytes):
        for line in data.decode().splitlines():
            self._handle(line.strip())

    def readline(self) -> bytes:
        deadline = time.time() + 0.2
        while time.time() < deadline:
            self._tick()
            with self._lock:
                if self._out:
                    return self._out.popleft()
            time.sleep(0.02)
        return b""

    def close(self):
        pass

    # ---------- behaviour ----------
    def _say(self, *lines):
        with self._lock:
            for ln in lines:
                self._out.append((ln + "\n").encode())

    def _temps_line(self) -> str:
        parts = [f"T:{self.temps[0]:.2f}/{self.targets[0]:.2f}", f"B:{self.bed:.2f}/0.00", "A:30.00/0.00"]
        parts += [f"T{i}:{self.temps[i]:.2f}/{self.targets[i]:.2f}" for i in range(8)]
        parts += ["@:0 B@:0", f"C@:{28 + random.uniform(-0.3, 0.3):.2f}"] + [f"@{i}:0" for i in range(8)]
        return " ".join(parts)

    _next_m73 = 0.0

    def _tick(self):
        if self.printing and os.environ.get("INDX_SIM_M73_ECHO") == "1" and time.time() >= self._next_m73:
            self._next_m73 = time.time() + 4
            pct = min(99, self.pos // 10000)
            self._say(f"echo: M73 Progress: {pct}%; Time left: {max(1, 95 - pct)}m;")
        if self.autoreport and time.time() >= self._next_report:
            self._next_report = time.time() + self.autoreport
            self._say(" " + self._temps_line())
        for i in range(8):
            self.temps[i] += (max(self.targets[i], 24.0) - self.temps[i]) * 0.05 + random.uniform(-0.2, 0.2)
        if self._job:
            kind, tool, material, finish, last = self._job
            now = time.time()
            if now >= finish:
                if kind == "load":
                    self.loaded[tool] = material
                else:
                    self.loaded[tool] = None
                self.targets[tool] = 0
                self._job = None
                self._say("ok")
            elif now - last >= 1.0:
                self._job = (kind, tool, material, finish, now)
                self._say("busy: paused for user")

    def _handle(self, line: str):
        if not line:
            return
        cmd = line.split()[0]
        if cmd == "M115":
            self._say("FIRMWARE_NAME:Prusa-Firmware-Buddy 7.0.0+sim (Github) SOURCE_CODE_URL:https://github.com/prusa3d/Prusa-Firmware-Buddy "
                      "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa-COREONEINDX EXTRUDER_COUNT:8", "Cap:EEPROM:0", "ok")
        elif cmd == "M105":
            # Like the real INDX: a bare M105 needs a picked-up tool (only while printing here),
            # and during a print "M105 T<n>" fails unless the print uses tool n (here: only T2).
            m_t = re.search(r"\bT(\d)", line)
            if (not m_t and not self.printing) or (m_t and self.printing and m_t.group(1) != "2"):
                self._say(f"echo: Invalid extruder {m_t.group(1) if m_t else -1}")
                return
            # Real format: "ok T:24.10/0.00 B:24.30/0.00 X:... T0:24.10/0.00 ... C@:28.00"
            self._say("ok " + self._temps_line())
        elif cmd == "M46":
            self._say("192.168.1.50", "ok")
        elif cmd == "M31":
            self._say("echo:Print time: 1h 7m 12s" if self.printing else "echo:Print time: 0s", "ok")
        elif cmd == "M73" and line.strip() == "M73":
            if self.printing:
                pct = min(99, self.pos // 10000)
                self._say(f"echo: M73 Progress: {pct}%;", f" Time left: {max(1, 95 - pct)}m;", "ok")
            else:
                self._say("echo: M73 Progress: 0%;", "ok")
        elif cmd == "M118":
            self._say(line.split(" ", 1)[1] if " " in line else "", "ok")
        elif cmd == "M155":
            m_s = re.search(r"\bS(\d+)", line)
            self.autoreport = int(m_s.group(1)) if m_s else 0
            self._say("ok")
        elif cmd == "M27" and " C" in line:
            self._say("Current file: /usb/BENCHY~1.BGC", "ok")
        elif cmd == "M27":
            if self.printing:
                self.pos = min(self.pos + 4000, 1_000_000)
                self._say(f"SD printing byte {self.pos}/1000000", "ok")
            else:
                self._say("Not SD printing", "ok")
        elif cmd == "M865":
            m_i = re.search(r"\bI(\d)", line)
            m_s = re.search(r'S"([^"]*)"', line)
            m_l = re.search(r"\bL(\d)", line)
            if m_s and m_l:
                name = m_s.group(1)
                if name.upper() not in PRESETS:
                    self._say("echo:Filament type invalid or not specified.", "ok")
                    return
                self.loaded[int(m_l.group(1))] = name.upper()
                self._say(f"name:{name.upper()}", f"nozzle_temperature:{PRESETS[name.upper()]}", "ok")
            elif m_i:
                mat = self.loaded[int(m_i.group(1))]
                if mat:
                    self._say(f"name:{mat}", f"nozzle_temperature:{PRESETS[mat]}", "heatbed_temperature:60",
                              "is_abrasive:0", "requires_filtration:0", "ok")
                else:
                    self._say("ok")
            else:
                self._say("ok")
        elif cmd in ("M701", "M702"):
            tool = int(re.search(r"\bT(\d)", line).group(1))
            mat = None
            if cmd == "M701":
                mat = re.search(r'S"([^"]*)"', line).group(1).upper()
                self.targets[tool] = PRESETS.get(mat, 215)
            else:
                self.targets[tool] = PRESETS.get(self.loaded[tool] or "PLA", 215)
            now = time.time()
            self._job = ("load" if cmd == "M701" else "unload", tool, mat, now + 6, now)
        elif cmd == "SIM_PRINT":   # test hook: toggle a fake print
            self.printing = not self.printing
            self.pos = 0
            self._say("ok")
        else:
            self._say("ok")
