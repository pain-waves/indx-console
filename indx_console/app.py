# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Web server for the INDX filament console.

  INDX_PORT=/dev/ttyACM0   force a serial port (default: auto-detect the Prusa USB device)
  INDX_SIMULATE=1          use a simulated printer
  INDX_DB=path             spool database (default ~/.local/share/indx-console/spools.db)
  INDX_CONFIG=path         settings file (default ~/.config/indx-console/config.json)
"""
from __future__ import annotations

import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import display
from .controller import Controller
from .printer import MATERIALS, CommandError, Printer
from .sensors import SensorListener
from .settings import Settings
from .store import Store
from .wifi import WifiLink

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

HERE = Path(__file__).parent
STATIC = HERE / "static"

settings = Settings()
store = Store(Path(os.environ.get("INDX_DB", Path.home() / ".local/share/indx-console/spools.db")))


def _remember(materials):
    store.set_last_materials(dict(enumerate(materials)))


def _printer_ip(ip):
    """The printer reported its address over USB: use it for the Wi-Fi backup unless one was typed in."""
    cfg = settings.get()
    if cfg.get("printer_host_auto", True) and cfg.get("printer_host") != ip:
        settings.update({"printer_host": ip, "printer_host_auto": True})


if os.environ.get("INDX_SIMULATE") == "1":
    from .simulator import SimPrinter
    printer = Printer(transport_factory=SimPrinter, on_materials=_remember, on_ip=_printer_ip)
else:
    printer = Printer(port=os.environ.get("INDX_PORT") or None, on_materials=_remember, on_ip=_printer_ip)

wifi = WifiLink(settings.get)
sensors = SensorListener(port=int(os.environ.get("INDX_METRICS_PORT", "8514")))
controller = Controller(printer, wifi, store, sensors, settings.get)


@asynccontextmanager
async def lifespan(_app):
    printer.start()
    wifi.start()
    sensors.start()
    controller.start()
    display.set_brightness(settings.get()["brightness"])
    yield
    controller.stop()
    wifi.stop()
    printer.stop()


app = FastAPI(title="INDX filament console", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def state():
    data = controller.state()
    data["materials"] = MATERIALS
    data["settings"] = settings.public()
    data["display_available"] = display.available()
    data["hostname"] = socket.gethostname()
    return data


def _tool(n: int) -> int:
    if not 1 <= n <= 8:
        raise HTTPException(404, "Tools are numbered 1 to 8")
    return n - 1


def _run(fn, *args):
    try:
        fn(*args)
    except (CommandError, ValueError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


# ----- loading -----
@app.post("/api/tools/{n}/plan")
def plan(n: int, body: dict = Body(...)):
    return _run(controller.plan_load, _tool(n), body.get("material"), body.get("spool") or {}, bool(body.get("start_now")))


@app.post("/api/batch")
def batch(body: dict = Body(...)):
    try:
        tools = [int(n) - 1 for n in body.get("tools") or []]
        done = controller.batch_load(body.get("material"), tools, body.get("spool") or {}, bool(body.get("start_now", True)))
    except (CommandError, ValueError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "tools": [t + 1 for t in done]}


@app.post("/api/batch-unload")
def batch_unload(body: dict = Body(...)):
    try:
        done = controller.batch_unload([int(n) - 1 for n in body.get("tools") or []])
    except (CommandError, ValueError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "tools": [t + 1 for t in done]}


@app.post("/api/sensors/setup")
def sensors_setup():
    try:
        ip = controller.setup_sensors()
    except CommandError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "message": f"Confirm on the printer's screen to send sensor data to {ip}."}


@app.post("/api/tools/{n}/start")
def start(n: int):
    return _run(controller.start_load, _tool(n))


@app.delete("/api/tools/{n}/plan")
def cancel(n: int):
    return _run(controller.cancel_load, _tool(n))


@app.post("/api/tools/{n}/unload")
def unload(n: int):
    return _run(controller.unload, _tool(n))


@app.post("/api/tools/{n}/material")
def set_material(n: int, body: dict = Body(...)):
    return _run(controller.set_material, _tool(n), body.get("material"))


# ----- spool details -----
@app.put("/api/tools/{n}/spool")
def save_spool(n: int, body: dict = Body(...)):
    try:
        return store.save_spool_for_tool(_tool(n), body)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Check the numbers: {e}") from e


@app.delete("/api/tools/{n}/spool")
def clear_spool(n: int):
    store.clear_tool(_tool(n))
    return {"ok": True}


# ----- settings, display, diagnostics -----
@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    try:
        out = settings.update(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    if "brightness" in body:
        display.set_brightness(out["brightness"])
    return out


@app.post("/api/settings/fetch-address")
def fetch_address():
    """Read the printer's network address over USB (M46) and save it for the Wi-Fi backup."""
    import time as _t
    try:
        printer.request_ip()
    except CommandError as e:
        raise HTTPException(409, str(e)) from e
    before = settings.get()["printer_host"]
    deadline = _t.time() + 8
    while _t.time() < deadline and printer._ip_wanted:
        _t.sleep(0.2)
    ip = printer.state.ip_address
    if not ip:
        raise HTTPException(409, "The printer didn't report an address. Is it connected to your network?")
    settings.update({"printer_host": ip, "printer_host_auto": True})
    return {"ok": True, "address": ip,
            "message": f"Printer address {ip}." + (" No change." if ip == before else " Saved.")}


@app.post("/api/settings/test-wifi")
def test_wifi(body: dict = Body(default={})):
    cfg = settings.get()
    host = (body.get("printer_host") or cfg["printer_host"]).strip()
    key = (body.get("api_key") or cfg["api_key"]).strip()
    probe = WifiLink(lambda: {"printer_host": host, "api_key": key})
    try:
        d = probe.status_once()
        return {"ok": True, "message": f"Connected. Printer is {d.get('printer', {}).get('state', 'reachable').lower()}."}
    except Exception as e:  # noqa: BLE001
        msg = "Wrong PrusaLink password" if "401" in str(e) else f"Could not reach the printer: {e}"
        return {"ok": False, "message": msg}


@app.post("/api/display/sleep")
def display_sleep():
    return {"ok": display.sleep()}


@app.post("/api/display/wake")
def display_wake():
    return {"ok": display.wake(settings.get()["brightness"])}


@app.post("/api/exit")
def exit_console():
    display.wake(settings.get()["brightness"])
    return {"ok": display.exit_kiosk()}


@app.get("/api/log")
def raw_log():
    return {"lines": printer.raw_lines(120)}


@app.post("/api/refresh")
def refresh():
    printer.request_refresh()
    return {"ok": True}
