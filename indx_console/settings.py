# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Console settings, kept in ~/.config/indx-console/config.json."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULTS = {
    "brightness": 80,        # percent
    "sleep_minutes": 5,      # 0 = never
    "printer_host": "",      # Wi-Fi backup: printer IP or hostname
    "printer_host_auto": True,  # keep printer_host updated from the printer's own report (M46)
    "api_key": "",           # Wi-Fi backup: PrusaLink password
    "auto_start_on_sensor": True,  # start a set-up load when its side sensor sees filament
}
SLEEP_CHOICES = [1, 2, 5, 10, 30, 0]


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get("INDX_CONFIG", Path.home() / ".config/indx-console/config.json"))
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        try:
            self._data.update(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - a damaged file falls back to defaults
            pass

    def get(self) -> dict:
        with self._lock:
            return dict(self._data)

    def public(self) -> dict:
        d = self.get()
        d["has_api_key"] = bool(d.pop("api_key"))
        d["sleep_choices"] = SLEEP_CHOICES
        return d

    def update(self, changes: dict) -> dict:
        clean = {}
        if "brightness" in changes:
            clean["brightness"] = max(5, min(100, int(changes["brightness"])))
        if "sleep_minutes" in changes:
            v = int(changes["sleep_minutes"])
            if v not in SLEEP_CHOICES:
                raise ValueError("Pick one of the listed sleep times")
            clean["sleep_minutes"] = v
        if "printer_host" in changes:
            clean["printer_host"] = str(changes["printer_host"]).strip()[:100]
            # typing an address by hand stops automatic updates; clearing it turns them back on
            clean["printer_host_auto"] = bool(changes.get("printer_host_auto", not clean["printer_host"]))
        if "api_key" in changes and changes["api_key"] is not None:
            clean["api_key"] = str(changes["api_key"]).strip()[:100]
        if "auto_start_on_sensor" in changes:
            clean["auto_start_on_sensor"] = bool(changes["auto_start_on_sensor"])
        with self._lock:
            self._data.update(clean)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        return self.public()
