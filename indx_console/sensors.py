# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Side filament sensors, received from the printer's metrics stream.

The INDX firmware records each side sensor as the metric `side_fsensor` and, once
pointed at this Pi with `M334 <pi-ip> 8514` and switched on with
`M331 side_fsensor`, sends lines like:

    side_fsensor,n=3 st=2i 1234

n = tool index (0-7), st = sensor state: 2 = filament present, 3 = no filament.
All eight sensors share one rate limit in the firmware, so some tools may report
rarely; `counts` shows how often each tool has been heard from.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time

log = logging.getLogger("indx.sensors")

METRICS_PORT = 8514
STATE_NAMES = {0: "not ready", 1: "not calibrated", 2: "filament", 3: "empty", 4: "not connected", 5: "disabled"}
_RE = re.compile(rb"side_fsensor,n=(\d+) st=(\d+)i")


class SensorListener:
    def __init__(self, on_change=None, port: int = METRICS_PORT):
        self.port = port
        self._on_change = on_change         # callback(tool, old_state, new_state)
        self.state: dict[int, int] = {}
        self.seen: dict[int, float] = {}
        self.counts: dict[int, int] = {}
        self.last_packet = 0.0
        self.error: str | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="sensors", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            self.error = f"Can't listen on port {self.port}: {e}"
            log.warning(self.error)
            return
        while True:
            try:
                data, _ = sock.recvfrom(4096)
            except OSError:
                continue
            self.feed(data)

    def feed(self, data: bytes):
        now = time.time()
        changes = []
        with self._lock:
            self.last_packet = now
            for m in _RE.finditer(data):
                tool, st = int(m.group(1)), int(m.group(2))
                if not 0 <= tool < 8:
                    continue
                old = self.state.get(tool)
                self.state[tool] = st
                self.seen[tool] = now
                self.counts[tool] = self.counts.get(tool, 0) + 1
                if old != st:
                    changes.append((tool, old, st))
        for c in changes:
            if self._on_change:
                try:
                    self._on_change(*c)
                except Exception:  # noqa: BLE001
                    log.exception("sensor change handler failed")

    def tool_view(self, tool: int, fresh_s: float = 15.0) -> dict:
        with self._lock:
            st, seen = self.state.get(tool), self.seen.get(tool, 0.0)
        live = st is not None and time.time() - seen < fresh_s
        return {"state": STATE_NAMES.get(st) if st is not None else None, "has_filament": st == 2 if live else None,
                "live": live, "age_s": round(time.time() - seen, 1) if seen else None}

    def summary(self) -> dict:
        with self._lock:
            counts = dict(self.counts)
            last = self.last_packet
        return {"receiving": bool(last) and time.time() - last < 15, "last_packet_age_s": round(time.time() - last, 1) if last else None,
                "counts": {str(k + 1): v for k, v in sorted(counts.items())}, "error": self.error, "port": self.port}


def local_ip(towards: str | None = None) -> str:
    """The Pi's address on the network that reaches `towards` (or the internet)."""
    target = (towards or "8.8.8.8").replace("http://", "").split("/")[0].split(":")[0] or "8.8.8.8"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
