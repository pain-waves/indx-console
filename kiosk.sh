#!/usr/bin/env bash
# Copyright (c) 2026 Pain Printing. Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE).
# Commercial use requires permission. Unofficial project, not affiliated with Prusa Research.
# Opens the console full-screen on the touchscreen. Used at login and by the desktop icon.
PORT=8080
for _ in $(seq 1 60); do
  curl -s -o /dev/null "http://localhost:$PORT/api/state" && break
  sleep 1
done
BROWSER="$(command -v chromium || command -v chromium-browser)"
exec "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic \
  --disable-session-crashed-bubble --overscroll-history-navigation=0 \
  --touch-events=enabled "http://localhost:$PORT/?kiosk=1"
