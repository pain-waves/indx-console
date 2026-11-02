# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Ties the USB link, the Wi-Fi backup and the load list together.

Load list: you set up a load for any number of tools (material and spool details),
then tap Start on whichever tool you are about to feed, in any order. Started
tools run one at a time; a tool started while another is loading waits its turn.

USB is always preferred. Wi-Fi (PrusaLink) is used only while USB is down.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field

from .printer import TOOL_COUNT, CommandError, Printer, check_material
from .sensors import SensorListener, local_ip
from .store import Store
from .wifi import BUSY_STATES, WifiLink

log = logging.getLogger("indx.controller")

IDLE_WIFI_STATES = {"IDLE", "FINISHED", "STOPPED", "READY"}


@dataclass
class Plan:
    tool: int
    material: str
    spool: dict = field(default_factory=dict)
    status: str = "ready"        # ready | queued | running | failed
    kind: str = "load"           # load | unload
    message: str | None = None
    via: str | None = None       # usb | wifi
    started: float = 0.0


class Controller:
    def __init__(self, printer: Printer, wifi: WifiLink, store: Store,
                 sensors: SensorListener | None = None, get_settings=lambda: {}):
        self.printer, self.wifi, self.store = printer, wifi, store
        self.sensors, self._settings = sensors, get_settings
        if sensors:
            sensors._on_change = self.on_sensor
        self.plans: dict[int, Plan] = {}
        self.order: list[int] = []           # tools waiting their turn (run order comes from queue_order())
        self.wifi_job: dict | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="controller", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------- link selection ----------
    def link(self) -> str | None:
        if self.printer.state.connected:
            return "usb"
        if self.wifi.state.reachable:
            return "wifi"
        return None

    def can_act(self) -> tuple[bool, str | None]:
        link = self.link()
        if link == "usb":
            ok, why = self.printer.can_act()
        elif link == "wifi":
            w = self.wifi.state
            if w.printer_state in ("PRINTING", "PAUSED"):
                ok, why = False, "A print is running"
            elif self.wifi_job or w.printer_state not in IDLE_WIFI_STATES:
                ok, why = False, "The printer is busy"
            else:
                ok, why = True, None
        else:
            ok, why = False, "Printer not connected (USB or Wi-Fi)"
        if ok and any(p.status == "running" for p in self.plans.values()):
            ok, why = False, "Another load is in progress"
        return ok, why

    def _printing(self) -> bool:
        if self.link() == "usb":
            return self.printer.state.printing
        return self.wifi.state.printer_state in ("PRINTING", "PAUSED")

    def _known_material(self, tool: int) -> str | None:
        if self.link() == "usb":
            return self.printer.state.tools[tool].material
        return self.store.last_materials().get(tool)

    # ---------- load list ----------
    def plan_load(self, tool: int, material: str, spool: dict | None, start_now: bool):
        material = check_material(material).upper()
        with self._lock:
            if self._printing():
                raise CommandError("A print is running. Tools are locked until it finishes.")
            p = self.plans.get(tool)
            if p and p.status == "running":
                raise CommandError("This tool is already loading")
            if self._known_material(tool):
                raise CommandError("This tool already has filament. Unload it first.")
            self.plans[tool] = Plan(tool, material, dict(spool or {}))
            if tool in self.order:
                self.order.remove(tool)
            if start_now:
                self.start_load(tool)

    def start_load(self, tool: int):
        with self._lock:
            p = self.plans.get(tool)
            if not p:
                raise CommandError("Set up the load for this tool first")
            if p.status in ("queued", "running"):
                return
            if not self.link():
                raise CommandError("Printer not connected (USB or Wi-Fi)")
            if self._printing():
                raise CommandError("A print is running. The load stays set up; tap Start after the print.")
            p.status, p.message = "queued", None
            self.order.append(tool)

    def batch_load(self, material: str, tools: list[int], spool: dict | None, start_now: bool) -> list[int]:
        """One material for several tools. Started loads run in numerical tool order."""
        material = check_material(material).upper()
        tools = sorted({int(t) for t in tools})
        if not tools:
            raise ValueError("Pick at least one tool")
        with self._lock:
            problems = []
            for t in tools:
                if not 0 <= t < TOOL_COUNT:
                    raise ValueError("Tools are numbered 1 to 8")
                p = self.plans.get(t)
                if p and p.status in ("queued", "running"):
                    problems.append(f"tool {t + 1} is already loading")
                elif self._known_material(t):
                    problems.append(f"tool {t + 1} already has filament")
            if problems:
                raise CommandError("Can't batch load: " + ", ".join(problems))
            if self._printing():
                raise CommandError("A print is running. Batch load after it finishes.")
            for t in tools:
                self.plan_load(t, material, spool, start_now)
        return tools

    def batch_unload(self, tools: list[int]) -> list[int]:
        """Unload several tools, one after another in numerical order."""
        tools = sorted({int(t) for t in tools})
        if not tools:
            raise ValueError("Pick at least one tool")
        with self._lock:
            if not self.link():
                raise CommandError("Printer not connected (USB or Wi-Fi)")
            problems = []
            for t in tools:
                if not 0 <= t < TOOL_COUNT:
                    raise ValueError("Tools are numbered 1 to 8")
                p = self.plans.get(t)
                if p and p.status in ("queued", "running"):
                    problems.append(f"tool {t + 1} is busy")
                elif not self._known_material(t):
                    problems.append(f"tool {t + 1} is empty")
            if problems:
                raise CommandError("Can't batch unload: " + ", ".join(problems))
            if self._printing():
                raise CommandError("A print is running. Unload after it finishes.")
            for t in tools:
                self.plans[t] = Plan(t, self._known_material(t) or "", kind="unload", status="queued")
                if t in self.order:
                    self.order.remove(t)
                self.order.append(t)
        return tools

    def on_sensor(self, tool: int, old: int | None, new: int):
        """Filament appeared at a tool's side sensor: start that tool's load if one is set up."""
        if new != 2 or old != 3:
            return
        if not self._settings().get("auto_start_on_sensor", True):
            return
        with self._lock:
            p = self.plans.get(tool)
            if p and p.status == "ready":
                try:
                    self.start_load(tool)
                    p.message = "Filament detected"
                except CommandError as e:
                    p.message = str(e)

    def setup_sensors(self) -> str:
        """Point the printer's metrics stream at this Pi. The printer asks to confirm on its screen."""
        if not self.printer.state.connected:
            raise CommandError("Connect the printer over USB first")
        ip = local_ip(self._settings().get("printer_host") or None)
        port = self.sensors.port if self.sensors else 8514
        self.printer.request_raw(f"M334 {ip} {port}", timeout=300)
        self.printer.request_raw("M331 side_fsensor")
        return ip

    def cancel_load(self, tool: int):
        with self._lock:
            p = self.plans.get(tool)
            if p and p.status == "running":
                raise CommandError("This tool is loading now. Finish or stop it on the printer.")
            self.plans.pop(tool, None)
            if tool in self.order:
                self.order.remove(tool)

    # ---------- direct actions ----------
    def unload(self, tool: int):
        with self._lock:
            ok, why = self.can_act()
            if not ok:
                raise CommandError(why)
            if self.link() == "usb":
                self.printer.request_unload(tool)
            else:
                self._wifi_run("unload", tool, None, [f"M702 T{tool}"])

    def set_material(self, tool: int, material: str):
        material = check_material(material).upper()
        with self._lock:
            ok, why = self.can_act()
            if not ok:
                raise CommandError(why)
            if self.link() == "usb":
                self.printer.request_set_material(tool, material)
            else:
                self._wifi_run("set", tool, material, [f'M865 S"{material}" L{tool}'])

    def _wifi_run(self, kind: str, tool: int, material: str | None, lines: list[str]):
        try:
            self.wifi.run_gcode("indx-console.gcode", lines)
        except Exception as e:  # noqa: BLE001
            raise CommandError(f"Wi-Fi send failed: {e}") from e
        self.wifi_job = {"kind": kind, "tool": tool, "material": material,
                         "started": time.time(), "seen_busy": False}

    # ---------- background ----------
    def _run(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._tick()
            except Exception:  # noqa: BLE001
                log.exception("controller tick failed")
            self._stop.wait(0.5)

    def _tick(self):
        now = time.time()
        self._check_wifi_job(now)
        running = next((p for p in self.plans.values() if p.status == "running"), None)
        if running and running.via == "usb":
            s = self.printer.state
            t = s.tools[running.tool]
            if not s.connected:
                self._fail(running, "USB disconnected during the load. Check the printer.")
            elif t.activity is None and now - running.started > 1.5:
                if running.kind == "unload":
                    if not t.material:
                        self._succeed(running)
                    else:
                        self._fail(running, "The unload didn't finish. Check the printer, then tap Retry.")
                elif t.material:
                    self._succeed(running)
                else:
                    self._fail(running, "The load didn't finish. Check the printer, then tap Retry.")
        elif not running and self.order:
            ok, _ = self.can_act()
            if ok:
                nxt = self.queue_order()[0]
                self.order.remove(nxt)
                p = self.plans.get(nxt)
                if p:
                    self._launch(p, now)

    def _sensor_has_filament(self, tool: int) -> bool:
        if not self.sensors:
            return False
        return bool(self.sensors.tool_view(tool).get("has_filament"))

    def queue_order(self) -> list[int]:
        """Who goes next among the waiting tools, worked out fresh every time:
        tools whose side sensor already sees filament first, then the rest, each in tool-number order.
        Only loads jump ahead: an unload always has filament at its sensor, so unloads keep number order."""
        def ahead(t):
            p = self.plans.get(t)
            return bool(p and p.kind == "load" and self._sensor_has_filament(t))
        return sorted(self.order, key=lambda t: (not ahead(t), t))

    def _launch(self, p: Plan, now: float):
        p.started, p.status, p.message = now, "running", None
        try:
            if self.link() == "usb":
                p.via = "usb"
                if p.kind == "unload":
                    self.printer.request_unload(p.tool)
                else:
                    self.printer.request_load(p.tool, p.material)
            else:
                p.via = "wifi"
                if p.kind == "unload":
                    self._wifi_run("unload", p.tool, None, [f"M702 T{p.tool}"])
                else:
                    self._wifi_run("load", p.tool, p.material, [f'M701 T{p.tool} S"{p.material}"'])
        except Exception as e:  # noqa: BLE001
            self._fail(p, str(e))

    def _succeed(self, p: Plan):
        if p.kind == "unload":
            self.store.clear_tool(p.tool)              # the spool is off this tool now
            self.store.set_last_materials({p.tool: None})
            self.plans.pop(p.tool, None)
            self.printer.request_refresh()
            return
        spool = {k: v for k, v in p.spool.items() if v not in ("", None)}
        if spool:
            self.store.clear_tool(p.tool)          # a newly loaded spool replaces the old record
            self.store.save_spool_for_tool(p.tool, {"material_type": p.material, **spool})
        self.store.set_last_materials({p.tool: p.material})
        self.plans.pop(p.tool, None)
        self.printer.request_refresh()

    def _fail(self, p: Plan, msg: str):
        p.status, p.message = "failed", msg

    def _check_wifi_job(self, now: float):
        j = self.wifi_job
        if not j:
            return
        w = self.wifi.state
        busy = w.printer_state in BUSY_STATES
        if busy:
            j["seen_busy"] = True
        elapsed = now - j["started"]
        done = ok = False
        msg = None
        if j["kind"] == "set" and elapsed > 8 and not busy:
            done = ok = True
        elif j["seen_busy"] and not busy and w.reachable:
            done = ok = True
        elif not j["seen_busy"] and elapsed > 45:
            done, msg = True, "The printer didn't start. Is a USB drive in the printer?"
        elif elapsed > 25 * 60:
            done, msg = True, "Timed out waiting for the printer."
        if not done:
            return
        self.wifi_job = None
        tool = j["tool"]
        p = self.plans.get(tool)
        if j["kind"] in ("load", "unload") and p and p.status == "running" and p.kind == j["kind"]:
            self._succeed(p) if ok else self._fail(p, msg)
        elif ok and j["kind"] == "unload":
            self.store.set_last_materials({tool: None})
        elif ok and j["kind"] == "set":
            self.store.set_last_materials({tool: j["material"]})

    # ---------- state for the UI ----------
    def state(self) -> dict:
        s = self.printer.snapshot()
        w = self.wifi.snapshot()
        link = "usb" if s.connected else ("wifi" if w.reachable else None)
        last = self.store.last_materials()
        out = {
            "link": link, "usb_connected": s.connected, "port": s.port, "firmware": s.firmware,
            "wifi": asdict(w), "filament_checked": s.filament_checked or link == "wifi" or bool(last),
        }
        now = time.time()
        if s.connected:
            out["print"] = {"file_name": s.file_name, "time_left_min": s.time_left_min,
                            "elapsed_s": int(now - s.print_started) if s.print_started else None}
        else:
            out["print"] = {"file_name": None,
                            "time_left_min": round(w.time_remaining / 60) if w.time_remaining else None,
                            "elapsed_s": w.time_printing}
        if link == "wifi":
            j = self.wifi_job
            out.update(printing=w.printer_state in ("PRINTING", "PAUSED") and not j, progress=w.progress,
                       bed_temp=w.bed_temp, bed_target=w.bed_target, chamber_temp=None,
                       busy=bool(j) or w.printer_state in ("BUSY", "ATTENTION"),
                       waiting_for_user=w.printer_state == "ATTENTION", last_error=None)
        else:
            out.update(printing=s.printing, progress=s.progress, bed_temp=s.bed_temp, bed_target=s.bed_target,
                       chamber_temp=s.chamber_temp, busy=s.busy, waiting_for_user=s.waiting_for_user,
                       last_error=s.last_error if s.connected or not (w.configured and w.last_error)
                       else f"{s.last_error or 'USB not connected'}. Wi-Fi backup: {w.last_error}")
        tools = []
        order = self.queue_order()
        for i in range(TOOL_COUNT):
            t = s.tools[i]
            if link == "usb":
                d = asdict(t)
                d["material_source"] = "printer"
                if not s.filament_checked:            # first read not done yet: show the last known
                    d["material"] = last.get(i)
                    d["material_source"] = "last_known" if i in last else None
            else:
                d = {"index": i, "material": last.get(i), "nozzle_temp": None, "nozzle_target": None,
                     "activity": None, "activity_note": None,
                     "material_source": "last_known" if i in last else None}
                j = self.wifi_job
                if j and j["tool"] == i:
                    d["activity"] = {"load": "loading", "unload": "unloading", "set": "setting"}[j["kind"]]
                    d["activity_note"] = "waiting_user" if w.printer_state == "ATTENTION" else "working"
            p = self.plans.get(i)
            d["plan"] = None if not p else {
                "material": p.material, "status": p.status, "message": p.message, "spool": p.spool, "kind": p.kind,
                "position": (order.index(i) + 1) if i in order else None,
                "filament_ready": self._sensor_has_filament(i),
            }
            d["spool"] = self.store.spool_for_tool(i)
            d["inlet"] = self.sensors.tool_view(i) if self.sensors else None
            tools.append(d)
        out["tools"] = tools
        ok, why = self.can_act()
        out["can_act"], out["blocked_reason"] = ok, why
        out["sensors"] = self.sensors.summary() if self.sensors else None
        out["queue_running"] = any(p.status in ("queued", "running") for p in self.plans.values())
        return out
