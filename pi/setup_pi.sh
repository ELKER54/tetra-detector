#!/bin/bash
# setup_pi.sh - one-off setup for the TETRA detector on Raspberry Pi OS Lite (64-bit)
# Run once on the Pi:  bash setup_pi.sh   then reboot.
set -e

echo "== Installing system packages (a few minutes on a Pi 3B)"
sudo apt-get update
# numpy/scipy from apt: prebuilt for the Pi, far quicker than pip building them.
sudo apt-get install -y rtl-sdr python3-venv python3-numpy python3-scipy tmux

echo "== Stopping the TV-tuner driver from grabbing the dongle"
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null

echo "== Letting your user open the dongle without sudo"
sudo usermod -aG plugdev "$USER"

echo "== Python environment"
# --system-site-packages so the venv uses the apt numpy/scipy above.
python3 -m venv --system-site-packages ~/sdrenv
~/sdrenv/bin/pip install --upgrade pip >/dev/null
~/sdrenv/bin/pip install "pyrtlsdr==0.5.1"

echo "== Checking"
# gaintest first: it stubs functions that Debian's older librtlsdr lacks
# (e.g. rtlsdr_set_dithering), exactly as live.py does.
cd "$(dirname "$0")"
~/sdrenv/bin/python - <<'EOF'
import numpy, scipy, platform
print("Python", platform.python_version(), "| numpy", numpy.__version__, "| scipy", scipy.__version__)
import gaintest, rtlsdr
print("pyrtlsdr imports OK (missing library functions stubbed:", gaintest.STUBBED or "none", ")")
EOF

echo
echo "Done. Now run:  sudo reboot"
echo "Then reconnect and follow step 6 in README-pi.md."
