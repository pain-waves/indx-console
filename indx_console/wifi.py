# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Wi-Fi backup link through PrusaLink, used only when USB is unavailable.

PrusaLink reports printer state, progress and the active tool's temperatures,
but not each tool's loaded filament. Loads, unloads and material changes are
sent the way we tested from PowerShell: a tiny G-code file uploaded with
Print-After-Upload, including the two lines that stop the compatibility popups.
A USB drive must be in the printer for this to work.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("indx.wifi")

POLL_S = 3.0
HEADER_LINES = ['M862.6 P"Input shaper"', 'M862.6 P"INDX lock"']
BUSY_STATES = {"PRINTING", "BUSY", "PAUSED", "ATTENTION"}


@dataclass
class WifiState:
    configured: bool = False
    reachable: bool = False
    printer_state: str | None = None     # IDLE, PRINTING, BUSY, ATTENTION, FINISHED, ...
    progress: float | None = None
    nozzle_temp: float | None = None     # active tool only
    nozzle_target: float | None = None
    bed_temp: float | None = None
    bed_target: float | None = None
    time_remaining: int | None = None    # seconds
    time_printing: int | None = None     # seconds
    last_error: str | None = None
    last_ok: float = 0.0


class WifiLink:
    def __init__(self, get_config):
        self._get_config = get_config        # returns dict with printer_host, api_key
        self.state = WifiState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wifi", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> WifiState:
        with self._lock:
            return WifiState(**self.state.__dict__)

    # ---------- requests ----------
    def _base(self) -> tuple[str, str]:
        cfg = self._get_config()
        host = (cfg.get("printer_host") or "").strip().rstrip("/")
        if host and not host.startswith("http"):
            host = "http://" + host
        return host, (cfg.get("api_key") or "").strip()

    def _request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None, timeout=4):
        host, key = self._base()
        if not host or not key:
            raise RuntimeError("Wi-Fi backup is not set up")
        req = urllib.request.Request(host + path, data=body, method=method,
                                     headers={"X-Api-Key": key, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return r.status, data

    def status_once(self) -> dict:
        status, data = self._request("GET", "/api/v1/status")
        return json.loads(data or b"{}")

    def run_gcode(self, name: str, lines: list[str]):
        body = ("\n".join(HEADER_LINES + lines) + "\n").encode("ascii")
        status, _ = self._request("PUT", f"/api/v1/files/usb/{name}", body,
                                  {"Print-After-Upload": "1", "Overwrite": "?1",
                                   "Content-Type": "application/octet-stream"}, timeout=10)
        if status not in (200, 201, 204):
            raise RuntimeError(f"Printer refused the upload ({status})")

    # ---------- polling ----------
    def _run(self):
        while not self._stop.is_set():
            host, key = self._base()
            configured = bool(host and key)
            with self._lock:
                self.state.configured = configured
            if configured:
                self._poll()
            self._stop.wait(POLL_S)

    def _poll(self):
        try:
            d = self.status_once()
            p, j = d.get("printer", {}), d.get("job") or {}
            with self._lock:
                s = self.state
                s.reachable, s.last_error, s.last_ok = True, None, time.time()
                s.printer_state = p.get("state")
                s.progress = j.get("progress")
                s.nozzle_temp, s.nozzle_target = p.get("temp_nozzle"), p.get("target_nozzle")
                s.bed_temp, s.bed_target = p.get("temp_bed"), p.get("target_bed")
                s.time_remaining, s.time_printing = j.get("time_remaining"), j.get("time_printing")
        except urllib.error.HTTPError as e:
            msg = "Wrong PrusaLink password (API key)" if e.code == 401 else f"PrusaLink error {e.code}"
            self._fail(msg)
        except Exception as e:  # noqa: BLE001
            self._fail(f"Printer not reachable over Wi-Fi ({e.__class__.__name__})")

    def _fail(self, msg: str):
        with self._lock:
            self.state.reachable = False
            self.state.last_error = msg
