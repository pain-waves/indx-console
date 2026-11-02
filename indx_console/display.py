# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Touch Display 2 backlight: brightness and sleep.

Uses the first device in /sys/class/backlight. install.sh adds a udev rule that
lets the console's user write to it without sudo.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("indx.display")
BACKLIGHT_ROOT = Path("/sys/class/backlight")


def _device() -> Path | None:
    try:
        devs = sorted(p for p in BACKLIGHT_ROOT.iterdir() if (p / "brightness").exists())
    except FileNotFoundError:
        return None
    return devs[0] if devs else None


def available() -> bool:
    return _device() is not None


def _write(path: Path, value: int) -> bool:
    try:
        path.write_text(str(value))
        return True
    except OSError as e:
        log.warning("cannot write %s: %s", path, e)
        return False


def set_brightness(percent: int) -> bool:
    dev = _device()
    if not dev:
        return False
    try:
        top = int((dev / "max_brightness").read_text().strip())
    except (OSError, ValueError):
        top = 255
    level = max(1, round(top * max(5, min(100, percent)) / 100))
    if (dev / "bl_power").exists():
        _write(dev / "bl_power", 0)
    return _write(dev / "brightness", level)


def sleep() -> bool:
    dev = _device()
    if not dev:
        return False
    if (dev / "bl_power").exists() and _write(dev / "bl_power", 4):
        return True
    return _write(dev / "brightness", 0)


def wake(percent: int) -> bool:
    return set_brightness(percent)


def exit_kiosk() -> bool:
    """Close the full-screen browser so the normal desktop shows."""
    r = subprocess.run(["pkill", "-f", "--", "--kiosk"], capture_output=True)
    return r.returncode == 0
