#!/bin/bash
# tetra_power.sh - boot-time power savers, run by tetra-power.service (as root)
#   LEDs off, HDMI off (best effort), and Wi-Fi off if the home network
#   isn't found within WAIT_S seconds. Wi-Fi always starts on at boot, so the
#   Pi can never be left unreachable at home; the phone app can also turn it on.
WAIT_S=300

for led in /sys/class/leds/ACT /sys/class/leds/PWR /sys/class/leds/led0 /sys/class/leds/led1; do
    if [ -d "$led" ]; then
        echo none > "$led/trigger" 2>/dev/null
        echo 0 > "$led/brightness" 2>/dev/null
    fi
done
echo "LEDs off"

# With the KMS graphics driver this may do nothing; harmless either way.
vcgencmd display_power 0 >/dev/null 2>&1 && echo "HDMI off (requested)"

# A block saved from last time must never carry over to a boot at home.
rfkill unblock wifi
for ((i = 0; i < WAIT_S; i += 5)); do
    if ip -4 addr show wlan0 2>/dev/null | grep -q "inet "; then
        echo "Wi-Fi connected; leaving it on"
        exit 0
    fi
    sleep 5
done
rfkill block wifi
echo "No home network after ${WAIT_S} s: Wi-Fi off to save power"
