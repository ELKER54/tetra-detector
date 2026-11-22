#!/bin/bash
# install_services.sh - make the Pi run the detector and Bluetooth link on power-up
# Run once on the Pi, from the folder with live.py:  bash install_services.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HOME/sdrenv/bin/python"

echo "== Bluetooth packages"
sudo apt-get install -y bluez
sudo rfkill unblock bluetooth || true
"$HOME/sdrenv/bin/pip" install "bless==0.3.0"

echo "== Services"
sudo tee /etc/systemd/system/tetra-ble.service >/dev/null <<EOF
[Unit]
Description=TETRA detector - Bluetooth link to the phone
After=bluetooth.service
Requires=bluetooth.service

[Service]
WorkingDirectory=$HERE
ExecStart=$PY -u $HERE/ble_server.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/tetra-live.service >/dev/null <<EOF
[Unit]
Description=TETRA detector - live detection (headless)
After=tetra-ble.service mnt-tetra.mount

[Service]
User=$USER
WorkingDirectory=$HERE
ExecStart=$PY -u $HERE/live.py --headless car
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/tetra-power.service >/dev/null <<UNIT
[Unit]
Description=TETRA detector - power savers (LEDs, HDMI, Wi-Fi off when away from home)
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash $HERE/tetra_power.sh

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now tetra-ble.service
sudo systemctl enable --now tetra-live.service
sudo systemctl enable tetra-power.service       # takes effect from the next boot

echo
echo "Done. Both start by themselves at every power-up from now on."
echo "  Check:  systemctl status tetra-ble tetra-live --no-pager"
echo "  Logs:   journalctl -u tetra-live -f      (Ctrl+C to stop watching)"
echo "  Pause detection (e.g. to use the menu or benchmark):  sudo systemctl stop tetra-live"
echo "  Results are saved on the USB stick (/mnt/tetra/runs), or ~/runs without it."
