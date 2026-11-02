# INDX filament console

A touchscreen console for a Prusa CORE One INDX, running on a Raspberry Pi 5 with the
7" Touch Display 2 in portrait. It talks to the printer over USB, with Wi-Fi (PrusaLink)
as a backup, and shows all 8 tools.

> **Unofficial project.** Not affiliated with or endorsed by Prusa Research. "Prusa",
> "CORE One" and "INDX" are trademarks of Prusa Research.
>
> **License:** free for personal, hobby, educational and nonprofit use under the
> PolyForm Noncommercial License 1.0.0 (see `LICENSE`). Commercial use, including selling
> the software, devices built with it, or kits, requires written permission from
> Pain Printing.

## What you need

- Raspberry Pi 5 (2 GB or more) with the official 27 W USB-C power supply
- Raspberry Pi Touch Display 2 (7")
- microSD card, 16 GB or larger, with Raspberry Pi OS (64-bit, with desktop)
- A USB-A to USB-C data cable, from one of the Pi's USB ports to the printer's USB-C port
  (the Pi's own USB-C port is for power only)
- No AI HAT, camera or other add-on is needed

## Install or update

Copy `indx-console.zip` to the Pi's home folder, then on the Pi:

```bash
cd ~
unzip -o indx-console.zip
cd indx-console
bash install.sh
sudo reboot
```

Running it again after copying a newer zip updates the console and keeps your spool
data and settings.

## The screen

- **Tool cards:** black = empty, green = loaded, yellow = being set up, loading or unloading.
- **Load** (on an empty tool) opens the load setup: pick the material and optional spool
  details, then **Add to load list** or **Load now**. The X cancels that tool's load.
- **Loading several tools:** set up every tool you want, then tap **Start** on each tool's
  card as you go to feed it, in any order. If a load is already running, the next one
  waits its turn. The yellow card tells you what to do at the printer.
- **Unload** (on a loaded tool) asks to confirm, then the printer walks you through it.
- **Batch load** (top right): pick one material, then any number of empty tools, or **All
  empty tools**. Each starts when the one before finishes.
- **Load order** (batch loads, or several single loads waiting in line): tools whose side
  sensor already sees filament go first, in number order, then the empty-sensor tools, in
  number order. The order is rechecked each time a load finishes, so feeding a waiting tool
  moves it ahead. Each yellow card shows its place in line as a large number (1 = loading
  now), and a waiting tool with filament shows **Filament sensed**.
- **Batch unload** (top right): pick any loaded tools, or **All loaded tools**. Unloads run
  in numerical order, each starting when the one before finishes. A tool waiting its turn
  can be cancelled from its card.
- **Tap a card** for spool details (brand, color, weights) and "Set material only".
- **Menu (☰):** brightness, sleep timer, Wi-Fi backup, printer log, and **Exit to desktop**.
  Reopen the console with the **INDX Console** icon on the desktop.
- **During a print** the console switches to a progress screen: a progress ring with time
  left, file name, time printing, bed and chamber, and each tool's material and temperature.
  All loading, unloading and spool changes are locked until the print ends. The X shows the
  tool cards (still locked); tap the blue banner or the Printing badge to bring the progress
  screen back.
- **Moon button** (top right) turns the screen off. A tap wakes it; during a print it wakes
  to the progress screen.
- The screen sleeps after the chosen time and wakes on a tap. The waking tap doesn't press
  anything. It stays awake while a load is running or the printer needs you.

## Wi-Fi backup

USB is always the main connection. While a Wi-Fi backup is set up, the console checks it in
the background, so if the USB cable is pulled it switches to Wi-Fi within a second or two,
and back to USB a few seconds after the cable returns.

- **Address:** filled in from the printer over USB (`M46`) and kept up to date. **Get
  address from printer** reads it on demand. You can also type one in by hand; it then
  stays as typed until you tap the button again.
- **PrusaLink password:** the printer has no USB command to read or set it, so enter it
  once. It's on the printer under Settings → Network → PrusaLink, and only changes if
  someone regenerates it.

When the console is using Wi-Fi:

- It shows the last materials the printer reported over USB. Per-tool temperatures
  aren't available over PrusaLink.
- Load, unload and set material are sent as a tiny G-code file that the printer saves and
  runs, the same way PrusaSlicer sends a print. The printer saves uploads to its USB flash
  drive, so **the drive must be plugged into the printer**, and each one appears as a job in
  Prusa Connect.
- **Test connection** asks the printer for its status with the address and password you
  typed, and says whether it answered. Nothing is saved until you tap **Save**.

## Troubleshooting

- **Menu → Show printer log** shows exactly what the printer sent.
- To see the raw replies directly:

  ```bash
  sudo systemctl stop indx-console
  cd ~/indx-console && .venv/bin/python -m indx_console.probe
  sudo systemctl start indx-console
  ```
- Service log: `journalctl -u indx-console -f`
- Test without the printer:
  `INDX_SIMULATE=1 .venv/bin/uvicorn indx_console.app:app --port 8081`, then open port 8081.

## Side filament sensors

The printer doesn't report its side sensors over USB or PrusaLink, but its metrics stream
does (`side_fsensor`). In **Menu → Filament sensors**, tap **Turn on sensor reporting**
and confirm on the printer's screen. That sends `M334 <Pi address> 8514`, which points the
stream at the Pi. The console sends `M331 side_fsensor` each time it connects.

With reporting on, a load that's been set up starts by itself when filament reaches that
tool's sensor, so you can feed tools in any order. **Start** still works if a sensor
doesn't report.

The sensor panel shows how many reports each tool has sent. All eight sensors share one
rate limit in the firmware, so some tools may report rarely or not at all. If only some
tools ever show reports, sensor-triggered starts will work only for those tools.

## How it talks to the printer

| Command | Used for |
|---|---|
| `M115` | Identify the printer on connect |
| `M105 T0` or `M105` | Nozzle temperatures for all tools, bed, chamber (every 3 s). Idle with tools parked only `M105 T0` answers; during a print, plain `M105` answers. The console uses whichever works. |
| `M27` / `M27 C` | Printing or idle, progress (every 5 s); file name when a print starts |
| `M73 Progress` lines | Progress and time left, echoed by the printer as a PrusaSlicer file runs |
| `M73` (bare) / `M31` | Time left and time printing on request, so they're right even if the console starts mid-print |
| `M46` | The printer's network address, used to fill in the Wi-Fi backup automatically |
| `M865 I<n>` | Material in each tool (every 30 s and after each action) |
| `M701 T<n> S"<material>"` | Load |
| `M702 T<n>` | Unload |
| `M865 S"<material>" L<n>` | Set material without moving filament |
| `M331 side_fsensor` | Switch on the side sensor stream (on every connect) |
| `M334 <ip> 8514` | Point the printer's metrics stream at the Pi (once, confirmed on the printer) |
| `M155 S3` | Printer sends temperatures by itself every 3 s, idle or printing |
| `M118 SYNC<n>` | A bookmark after every short request, so late replies during a print can't be matched to the wrong request |

Materials are read when idle and again as soon as a print ends; they aren't re-read during a print.

## OpenPrintTag later

Spool records use OpenPrintTag field names (`brand_name`, `material_type`,
`primary_color`, `actual_netto_full_weight`, `empty_container_weight`,
`consumed_weight`, `density`, `filament_diameter`), and remaining weight follows the
OpenPrintTag rule: full weight minus consumed weight.

## Credits

Built on [FastAPI](https://fastapi.tiangolo.com/) (MIT), [Uvicorn](https://www.uvicorn.org/)
(BSD) and [pySerial](https://github.com/pyserial/pyserial) (BSD), which are installed by
`install.sh` under their own licenses. Spool fields follow the
[OpenPrintTag](https://github.com/OpenPrintTag/openprinttag-specification) specification (MIT).

Copyright © 2026 Pain Printing.
