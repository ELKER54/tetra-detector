#!/bin/bash
# tetra_setup.sh - Pi setup menu: results stick, shutdown PIN, services, card protection
# Run on the Pi:  bash tetra_setup.sh
HERE="$(cd "$(dirname "$0")" && pwd)"
MNT=/mnt/tetra
LABEL=TETRADATA

protected() { findmnt -n -o FSTYPE / | grep -q overlay; }

need_unprotected() {
    if protected; then
        echo "The card is PROTECTED (read-only), so this change would vanish at reboot."
        echo "Choose 'Unprotect' first, let it reboot, then come back."
        return 1
    fi
}

prepare_stick() {
    need_unprotected || return
    mapfile -t sticks < <(lsblk -dpno NAME,TRAN | awk '$2=="usb"{print $1}')
    if [ ${#sticks[@]} -eq 0 ]; then
        echo "No USB stick found. Plug it in and try again."; return
    fi
    if [ ${#sticks[@]} -gt 1 ]; then
        echo "More than one USB disk found:"; lsblk -dpo NAME,SIZE,MODEL "${sticks[@]}"
        read -rp "Type the one to use (e.g. /dev/sda): " dev
    else
        dev=${sticks[0]}
    fi
    lsblk -dpo NAME,SIZE,MODEL "$dev"
    echo "EVERYTHING on $dev will be erased."
    read -rp "Type WIPE to continue: " ok
    [ "$ok" = "WIPE" ] || { echo "Cancelled."; return; }
    sudo umount "${dev}"* 2>/dev/null
    sudo wipefs -a "$dev" >/dev/null
    echo 'type=83' | sudo sfdisk -q "$dev"
    sleep 2
    part=$(lsblk -lpno NAME "$dev" | sed -n 2p)
    sudo mkfs.ext4 -q -F -L "$LABEL" "$part"
    sudo mkdir -p "$MNT"
    # nofail: the Pi still boots and detects without the stick, just without saved results.
    grep -q "LABEL=$LABEL" /etc/fstab || \
        echo "LABEL=$LABEL $MNT ext4 defaults,noatime,nofail,commit=5,x-systemd.device-timeout=10 0 2" \
        | sudo tee -a /etc/fstab >/dev/null
    sudo systemctl daemon-reload
    sudo mount "$MNT" && sudo mkdir -p "$MNT/runs" && sudo chown -R "$USER:$USER" "$MNT"
    echo "Stick ready. Results will be saved in $MNT/runs"
}

set_pin() {
    need_unprotected || return
    read -rsp "New shutdown PIN (digits, at least 6 recommended): " p1; echo
    read -rsp "Same again: " p2; echo
    [ -n "$p1" ] && [ "$p1" = "$p2" ] || { echo "Didn't match. Unchanged."; return; }
    sudo mkdir -p /etc/tetra
    printf '%s\n' "$p1" | sudo tee /etc/tetra/pin >/dev/null
    sudo chmod 600 /etc/tetra/pin
    sudo systemctl restart tetra-ble 2>/dev/null
    echo "PIN saved. Enter the same PIN in the phone app (Set shutdown PIN)."
}

install_services() {
    need_unprotected || return
    bash "$HERE/install_services.sh"
}

protect() {
    need_unprotected || return
    echo "After this the system part of the card is read-only and the Pi can lose"
    echo "power at any time without damage. Results still go to the USB stick."
    echo "To update files later, use 'Unprotect' first."
    read -rp "Protect and reboot now? y/n: " ok
    [ "$ok" = "y" ] || return
    sudo raspi-config nonint do_overlayfs 0 || return
    # By default overlayroot also wraps every fstab mount, which would put the
    # results stick in RAM too. recurse=0 protects the system card only.
    sudo mount -o remount,rw /boot/firmware 2>/dev/null
    sudo sed -i 's/overlayroot=tmpfs\([^:]\|$\)/overlayroot=tmpfs:recurse=0\1/' /boot/firmware/cmdline.txt
    if grep -q "overlayroot=tmpfs:recurse=0" /boot/firmware/cmdline.txt; then
        echo "Protection set (system only; USB stick stays writable). Rebooting."
        sudo reboot
    else
        echo "Could not set recurse=0 in /boot/firmware/cmdline.txt. Not rebooting."
        echo "Send Claude the output of: cat /boot/firmware/cmdline.txt"
    fi
}

unprotect() {
    protected || { echo "Already unprotected."; return; }
    read -rp "Unprotect (for updates) and reboot now? y/n: " ok
    [ "$ok" = "y" ] || return
    # Put the cmdline back to what raspi-config wrote, so it can find and remove it.
    sudo mount -o remount,rw /boot/firmware 2>/dev/null
    sudo sed -i 's/overlayroot=tmpfs:recurse=0/overlayroot=tmpfs/' /boot/firmware/cmdline.txt
    sudo raspi-config nonint do_overlayfs 1 && sudo reboot
}

trim_services() {
    need_unprotected || return
    echo "Switching off background jobs this detector doesn't need:"
    echo "  update checks, manual-page rebuild, card maintenance, swap writeback,"
    echo "  mDNS (<hostname>.local stops working; use the IP address), screen login."
    read -rp "Go ahead? y/n: " ok
    [ "$ok" = "y" ] || return
    for u in man-db.timer apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer \
             e2scrub_all.timer fstrim.timer rpi-zram-writeback.timer \
             avahi-daemon.service avahi-daemon.socket getty@tty1.service; do
        if sudo systemctl disable --now "$u" >/dev/null 2>&1; then
            echo "  off: $u"
        else
            echo "  (not present: $u)"
        fi
    done
    echo "Done. To update Pi OS later: Unprotect, then sudo apt update && sudo apt full-upgrade."
}

status() {
    echo "Card:        $(protected && echo 'PROTECTED (read-only)' || echo 'unprotected (writable)')"
    if mountpoint -q "$MNT" && [ "$(findmnt -n -o FSTYPE "$MNT")" = "overlay" ]; then
        echo "USB stick:   PROBLEM - wrapped in RAM, results lost at power-off."
        echo "             Unprotect (5), then Protect (4) again with this version."
    elif mountpoint -q "$MNT"; then
        echo "USB stick:   mounted, writable, $(df -h --output=avail "$MNT" | tail -1 | tr -d ' ') free"
    else
        echo "USB stick:   NOT mounted (results will be lost at power-off)"
    fi
    echo "Shutdown PIN: $([ -s /etc/tetra/pin ] && echo set || echo 'not set (button disabled)')"
    echo "Wi-Fi radio: $(rfkill list wifi 2>/dev/null | grep -q 'Soft blocked: yes' && echo OFF || echo on)"
    for s in tetra-ble tetra-live tetra-power; do
        printf '%-13s %s\n' "$s:" "$(systemctl is-active $s 2>/dev/null)"
    done
    echo "Power/heat:  $(vcgencmd get_throttled 2>/dev/null) $(vcgencmd measure_temp 2>/dev/null)"
}

while true; do
    echo
    echo "=== TETRA Pi setup ==="
    echo "1. Prepare the USB stick for results (erases it)"
    echo "2. Set the shutdown PIN"
    echo "3. Install or refresh the start-up services"
    echo "4. Protect the card (read-only, safe to pull the power)"
    echo "5. Unprotect the card (for updates)"
    echo "6. Status"
    echo "7. Switch off unneeded background services"
    echo "8. Quit"
    read -rp "> " c
    case "$c" in
        1) prepare_stick ;;
        2) set_pin ;;
        3) install_services ;;
        4) protect ;;
        5) unprotect ;;
        6) status ;;
        7) trim_services ;;
        8) exit 0 ;;
    esac
done
