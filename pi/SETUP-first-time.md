# Pi 3B: setup and benchmark

The goal is to find out whether a Pi 3B can run the detector in real time. This takes about 30-40 minutes, mostly waiting.

Everything is done from the Windows PC over Wi-Fi. The Pi needs no screen or keyboard.

## What you need

- The Pi 3B, a microSD card (16 GB or more, **it will be wiped**) and the SD card reader
- The RTL-SDR dongle and antenna
- Power, **as close to the car as you can get**: a USB-C phone charger plus the USB-C to micro-USB cable you plan to use in the car. The benchmark reports whether the power sags, so this tests the cable too.
- No heatsink needed for this test. The benchmark reports the Pi's temperature and whether it slowed down from heat. That tells us whether you need one for a car in summer.

## 1. Flash the card (on the PC)

1. Download and install **Raspberry Pi Imager** from raspberrypi.com/software.
2. In Imager, choose:
   - **Device:** Raspberry Pi 3
   - **OS:** Raspberry Pi OS (other) > **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** your SD card
3. When it offers to customise the settings, say yes and set:
   - **Hostname:** your choice, e.g. `tetra-pi`. This guide writes it as `<pi-host>`.
   - **Username and password:** your choice. Write them down. This guide writes it as `<user>`.
   - **Wi-Fi:** your home network name and password, **country GB**
   - **Time zone:** Europe/London
   - **SSH:** enabled, with password login
4. Write the card. Imager's screens change between versions, so if a label differs, look for the same setting.

## 2. First boot

Put the card in the Pi and plug the dongle into a Pi USB port, with the antenna on. Plug in the power last. Wait **3 minutes**, because the first boot is slow.

## 3. Connect from the PC

Open **PowerShell** and type:

```
ssh <user>@<pi-host>.local
```

Type `yes` to the fingerprint question, then your password. If `<pi-host>.local` isn't found, look up the Pi's IP address on your router's device list and use `ssh <user>@<that IP>` instead.

## 4. Copy the files over

Open a **second** PowerShell window, on the PC, not over SSH:

```
cd C:\Users\kiera\Desktop\Documents\pi
scp live.py detector.py gaintest.py replay.py setup_pi.sh <user>@<pi-host>.local:~/
```

## 5. Install (in the SSH window)

```
bash setup_pi.sh
sudo reboot
```

The reboot drops the SSH connection. Wait a minute and run `ssh <user>@<pi-host>.local` again.

## 6. Check the dongle

```
rtl_test -t
```

It should print `Found Rafael Micro R820T tuner`. `PLL not locked` is harmless, as before. If it says `usb_claim_interface error -6`, reboot again.

## 7. Run the benchmark

Run it inside `tmux`. Then a Wi-Fi blip that drops SSH won't kill the run.

```
tmux new -s tetra
cd ~
source ~/sdrenv/bin/activate
python live.py
```

Choose **5. Benchmark this machine (10 min)**. It runs the full detector with beeps off and prints a status line every 30 s. That line now also shows the Pi's temperature and throttle flags.

If SSH drops, reconnect and type `tmux attach -t tetra` to get back to it.

## 8. Send me

- The **Benchmark verdict** block at the end
- Two or three of the status lines
- What power supply and cable you used

## What the verdict means

| Verdict | Meaning |
|---------|---------|
| PASS | The Pi 3B is fine. Next is making it start on power-up and survive power cuts. |
| MARGINAL | It works but with little headroom. I'll look at cutting the work further. |
| FAIL | It's too slow as it stands. There are options (spreading the work over the Pi's 4 cores, or a Pi 4 or 5), and I'll lay them out. |
| POWER warning | The supply or cable can't deliver enough. That has to be fixed before the other results mean anything. |
