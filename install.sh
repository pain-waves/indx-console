#!/usr/bin/env bash
# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
# Installs or updates the INDX filament console on a Raspberry Pi 5
# (Raspberry Pi OS Bookworm or Trixie, desktop). Run from this folder:  bash install.sh
# Safe to run again after copying in a newer version.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
PORT=8080

echo "== Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip curl
sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser

echo "== Allowing $USER_NAME to use the printer's USB serial port"
sudo usermod -aG dialout "$USER_NAME"

echo "== Allowing the console to set screen brightness and sleep"
sudo tee /etc/udev/rules.d/90-indx-backlight.rules >/dev/null <<'EOF'
SUBSYSTEM=="backlight", ACTION=="add", RUN+="/bin/chmod a+w /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power"
EOF
sudo udevadm control --reload-rules
sudo chmod a+w /sys/class/backlight/*/brightness /sys/class/backlight/*/bl_power 2>/dev/null || true

echo "== Creating Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/kiosk.sh"

echo "== Installing the console service"
sudo tee /etc/systemd/system/indx-console.service >/dev/null <<EOF
[Unit]
Description=INDX filament console
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn indx_console.app:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable indx-console.service
sudo systemctl restart indx-console.service

echo "== Opening the console full-screen at login"
mkdir -p "$HOME/.config/labwc"
AUTOSTART="$HOME/.config/labwc/autostart"
touch "$AUTOSTART"
# remove any earlier console lines, then add the current launcher
sed -i '/indx-console/d;/unclutter --timeout/d;/localhost:8080/d' "$AUTOSTART"
echo "$APP_DIR/kiosk.sh &  # indx-console" >> "$AUTOSTART"

echo "== Adding an 'INDX Console' icon to reopen it after exiting"
DESKTOP_FILE="[Desktop Entry]
Type=Application
Name=INDX Console
Comment=Filament console for the CORE One INDX
Exec=$APP_DIR/kiosk.sh
Icon=applications-engineering
Terminal=false
Categories=Utility;"
mkdir -p "$HOME/.local/share/applications" "$HOME/Desktop"
echo "$DESKTOP_FILE" > "$HOME/.local/share/applications/indx-console.desktop"
echo "$DESKTOP_FILE" > "$HOME/Desktop/indx-console.desktop"
chmod +x "$HOME/Desktop/indx-console.desktop"

echo
echo "Done. Reboot to start the console on the touchscreen:  sudo reboot"
echo "From a phone or laptop on the same network:  http://$(hostname).local:$PORT"
