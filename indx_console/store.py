# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Spool records, stored with OpenPrintTag field names so tag data drops in later.

Remaining filament = actual_netto_full_weight - consumed_weight (the OpenPrintTag rule).
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

# OpenPrintTag-named fields we keep, plus a few console-only ones (source, uid, updated).
SPOOL_FIELDS = {
    "brand_name": str,
    "material_name": str,
    "material_type": str,            # e.g. PLA, PETG (also what we send to the printer)
    "primary_color": str,            # "#RRGGBB"
    "nominal_netto_full_weight": float,
    "actual_netto_full_weight": float,
    "empty_container_weight": float,
    "consumed_weight": float,
    "density": float,                # g/cm3
    "filament_diameter": float,      # mm
    "nominal_full_length": float,    # mm
}

DENSITY_DEFAULTS = {"PLA": 1.24, "PETG": 1.27, "ASA": 1.07, "ABS": 1.04, "PC": 1.20, "PVB": 1.08,
                    "HIPS": 1.04, "PP": 0.90, "FLEX": 1.21, "PA": 1.14, "PPS": 1.35, "PPA": 1.15}

SCHEMA = """
CREATE TABLE IF NOT EXISTS spools (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'manual',   -- manual | openprinttag
  tag_uid TEXT,                            -- filled in when a tag is read
  brand_name TEXT, material_name TEXT, material_type TEXT, primary_color TEXT,
  nominal_netto_full_weight REAL, actual_netto_full_weight REAL, empty_container_weight REAL,
  consumed_weight REAL DEFAULT 0, density REAL, filament_diameter REAL DEFAULT 1.75,
  nominal_full_length REAL,
  updated REAL
);
CREATE TABLE IF NOT EXISTS tool_spool (
  tool INTEGER PRIMARY KEY,
  spool_id TEXT REFERENCES spools(id)
);
CREATE TABLE IF NOT EXISTS tool_material (   -- last material the printer reported, for Wi-Fi mode
  tool INTEGER PRIMARY KEY,
  material TEXT,
  updated REAL
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def spool_for_tool(self, tool: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT s.* FROM tool_spool t JOIN spools s ON s.id = t.spool_id WHERE t.tool = ?", (tool,)
            ).fetchone()
        return _with_derived(dict(row)) if row else None

    def save_spool_for_tool(self, tool: int, fields: dict) -> dict:
        clean = {}
        for k, v in fields.items():
            if k not in SPOOL_FIELDS:
                continue
            if v in ("", None):
                clean[k] = None
                continue
            clean[k] = SPOOL_FIELDS[k](v)
        if "remaining_weight" in fields and fields["remaining_weight"] not in ("", None):
            full = clean.get("actual_netto_full_weight")
            if full is None:
                cur = self.spool_for_tool(tool)
                full = (cur or {}).get("actual_netto_full_weight")
            if full is not None:
                clean["consumed_weight"] = max(0.0, float(full) - float(fields["remaining_weight"]))
        mt = (clean.get("material_type") or "").upper() or None
        if mt:
            clean["material_type"] = mt
            clean.setdefault("density", DENSITY_DEFAULTS.get(mt))
        existing = self.spool_for_tool(tool)
        with self._lock:
            if existing:
                if clean:
                    sets = ", ".join(f"{k} = ?" for k in clean)
                    self._db.execute(f"UPDATE spools SET {sets}, updated = ? WHERE id = ?",
                                     (*clean.values(), time.time(), existing["id"]))
            else:
                sid = uuid.uuid4().hex
                cols = ["id", "updated", *clean.keys()]
                self._db.execute(f"INSERT INTO spools ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                 (sid, time.time(), *clean.values()))
                self._db.execute("INSERT OR REPLACE INTO tool_spool (tool, spool_id) VALUES (?, ?)", (tool, sid))
            self._db.commit()
        return self.spool_for_tool(tool)

    def clear_tool(self, tool: int):
        with self._lock:
            self._db.execute("DELETE FROM tool_spool WHERE tool = ?", (tool,))
            self._db.commit()

    def set_last_materials(self, materials: dict[int, str | None]):
        with self._lock:
            for tool, mat in materials.items():
                self._db.execute("INSERT OR REPLACE INTO tool_material (tool, material, updated) VALUES (?, ?, ?)",
                                 (tool, mat, time.time()))
            self._db.commit()

    def last_materials(self) -> dict[int, str | None]:
        with self._lock:
            rows = self._db.execute("SELECT tool, material FROM tool_material").fetchall()
        return {r["tool"]: r["material"] for r in rows}

    def add_usage(self, tool: int, grams: float):
        """For later: subtract a finished job's usage from the tool's spool."""
        s = self.spool_for_tool(tool)
        if not s:
            return
        with self._lock:
            self._db.execute("UPDATE spools SET consumed_weight = COALESCE(consumed_weight, 0) + ?, updated = ? WHERE id = ?",
                             (grams, time.time(), s["id"]))
            self._db.commit()


def _with_derived(s: dict) -> dict:
    full = s.get("actual_netto_full_weight") or s.get("nominal_netto_full_weight")
    used = s.get("consumed_weight") or 0.0
    s["remaining_weight"] = round(max(0.0, full - used), 1) if full else None
    s["remaining_pct"] = round(100 * s["remaining_weight"] / full) if full else None
    dens, dia = s.get("density"), s.get("filament_diameter") or 1.75
    if s["remaining_weight"] is not None and dens:
        area_cm2 = 3.14159265 * (dia / 20) ** 2          # mm diameter -> cm radius
        s["remaining_length_m"] = round(s["remaining_weight"] / dens / area_cm2 / 100, 1)
    else:
        s["remaining_length_m"] = None
    return s
