# Pi side

Runs on the Raspberry Pi 3B in the car. Full design: `../docs/techspec.md`.

## Runs all the time (systemd)

| File | What it does |
|---|---|
| `live.py` | The detector. `live.py --headless car` under `tetra-live.service`; run without arguments for the menu. |
| `detector.py` | Detection maths used by `live.py`. |
| `gaintest.py` | Gain test tool; also stubs functions Debian's librtlsdr lacks. `live.py` imports it first. |
| `tetra_link.py` | Pi-to-phone message formats. Must match the app's `Link.kt` (checked by `LinkTest`). |
| `ble_server.py` | Bluetooth LE link to the phone (`tetra-ble.service`, root). |
| `tetra_power.sh` | Boot power savers (`tetra-power.service`). |

## Setup and maintenance

| File | What it does |
|---|---|
| `setup_pi.sh` | One-off packages and Python venv (`~/sdrenv`). |
| `install_services.sh` | Installs the three systemd services. |
| `tetra_setup.sh` | Menu: USB stick, PIN, services, Protect/Unprotect (read-only root), status, switch off unneeded services. |
| `SETUP-first-time.md` | First-time flashing and benchmark guide. |

## Tools

| File | What it does |
|---|---|
| `replay.py` | Re-runs saved `detections.csv` through the current alert rules. |
| `survey.py` | Band survey. |
| `bench.py` | Speed benchmark. |
| `scanner.py` | Early wideband scanner. |
| `harness.py`, `inject.py` | Synthetic-signal test harness for `detector.py`. |

## Updating the Pi from this folder

1. `bash tetra_setup.sh`, then **5 (Unprotect)**. It reboots.
2. From the PC: `scp <changed files> <user>@<pi-host>.local:<detector folder>/`
3. `bash tetra_setup.sh`, then **3 (Install services)**, then **4 (Protect)**.

Detection only: nothing here demodulates or decodes TETRA, and it must stay that way.
