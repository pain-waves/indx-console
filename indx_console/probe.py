# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
"""Print exactly what the printer answers over USB, for troubleshooting.

Stop the console first, since only one program can use the port:
    sudo systemctl stop indx-console
    .venv/bin/python -m indx_console.probe
    sudo systemctl start indx-console
"""
import sys
import time

import serial

from .printer import find_printer_port

COMMANDS = ["M115", "M105 T0", "M27"] + [f"M865 I{i}" for i in range(8)]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_printer_port()
    if not port:
        print("No Prusa printer found on USB. Check the cable is in the printer's lower rear USB-C port.")
        return 1
    print(f"Printer found on {port}\n")
    with serial.Serial(port, 115200, timeout=0.3) as s:
        time.sleep(0.5)
        s.reset_input_buffer()
        for cmd in COMMANDS:
            print(f">>> {cmd}")
            s.write((cmd + "\n").encode())
            end = time.time() + 5
            while time.time() < end:
                line = s.readline().decode("ascii", "replace").rstrip()
                if not line:
                    continue
                print(f"    {line}")
                if line.startswith("ok"):
                    break
            else:
                print("    (no 'ok' within 5 seconds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
