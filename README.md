# TETRA detector

This is just a project I wanted to play around with and see if I could get something working. Do not rely on this working for you at all.

A Raspberry Pi and an RTL-SDR dongle listen to the UK Airwave (TETRA) uplink band, 380 to 385 MHz. When a nearby police, ambulance or fire radio transmits, an Android app beeps, shows how strong the signal is, and puts the same picture on Android Auto.

It only detects that a radio is transmitting. It measures signal energy and burst timing, and never demodulates, decodes or records what is said. Check the law where you live before you use it, and don't fiddle with the phone while driving: the car screen has big buttons for the few things you need.

## Who wrote this

Claude, Anthropic's AI model, wrote the code, the tests and the documentation, working in a long conversation with the repo owner. The owner did all the hardware work, ran every test in the car, and made the design calls. Expect rough edges: it has been tested on one Pi, one phone and one car.

## How it works

```
 RTL-SDR dongle --USB--> Raspberry Pi --Bluetooth LE--> Android phone --> Android Auto
 (380-385 MHz)           live.py: detection            TETRA Alert app      car screen
                         ble_server.py: link           sound, history,
                         USB stick: logs               map, widget
```

The dongle sees 2.4 MHz at a time, so the Pi hops between three windows 1.65 MHz apart to cover the band, spending 0.8 s on each. For each window it runs a 2048-point FFT and a CFAR threshold (5 dB over the local noise), and keeps signals about one TETRA channel (25 kHz) wide whose bursts last as long as TETRA bursts do (5.7 or 12.8 ms).

TETRA radios transmit on a fixed 56.667 ms frame grid, and most interference doesn't. A channel whose gaps stop fitting that grid is marked `unlocked` and stays silent. Three or more channels lighting up in one dwell counts as wideband interference, unless a channel already fits the grid.

Each channel then becomes an episode. The Pi tells the phone about new activity after 30 s of quiet, when the strength band goes up, or when the signal jumps 10 dB. While a channel stays busy it also sends the current strength every 2 s, which drives the repeat beeps and "Tetra clear".

Strength bands, in dB above the noise:

| Band | dB | Sound |
|---|---|---|
| weak | under 15 | hidden by default |
| medium | 15 to 30 | two pips (Motorway mode only, by default) |
| STRONG | over 30 | three high pips, always |

On a Pi 3B the maths runs on all four cores at around 70 % load.

## What you need

- An RTL-SDR Blog V3 dongle.
- An antenna for 380 to 400 MHz. Testing used Co-Star's [Midi Mag kit (MD-TET-5F)](https://www.co-star.co.uk/809/midi-mag-antenna-kit-md-tet-5f/), a magnetic roof whip for 380 to 430 MHz on 5 m of RG58. Their [TETRA Flexi-Strip (EF-S3-3F)](https://www.co-star.co.uk/16604/tetra-flexi-strip-antenna-380-420-mhz-ef-s3-3f/), which sticks inside a window, should also work but hasn't been tested. Both end in an FME connector and the dongle has an SMA socket, so you need an FME male to SMA male adaptor (the Flexi-Strip comes with one). A better antenna is the cheapest way to gain range.
- A Raspberry Pi 3B or newer, with a fan case. Without one, a Pi 3B reached 72 °C in a parked car; with one it idled at 37 °C at home.
- A USB stick for the logs.
- A steady 5 V supply of at least 2.5 A. A car USB-C port worked, but showed under-voltage at times. A proper 12 V to 5 V converter is better.
- An Android phone on Android 13 or newer. Android Auto is optional.

## Setting up the Pi

The Pi side lives in [`pi/`](pi/). Start with [`pi/SETUP-first-time.md`](pi/SETUP-first-time.md), which covers flashing Raspberry Pi OS Lite (64-bit), installing the packages and a quick speed test. After that, everything goes through one menu:

```
bash tetra_setup.sh
```

1. Prepare the USB stick. This wipes it, and you have to type `WIPE` to confirm.
2. Set the PIN. The phone needs the same one for shutdown, Wi-Fi on and log downloads.
3. Install the three services: `tetra-live` (detection), `tetra-ble` (Bluetooth) and `tetra-power` (LEDs, HDMI and Wi-Fi off away from home).
4. Protect. The SD card becomes read-only so pulling the power can't damage it, while the USB stick stays writable.
5. Unprotect, for updates.
6. Status.
7. Switch off services the detector doesn't need.

To update the Pi later: Unprotect, copy the new files over, run option 3, then Protect.

If the dongle hangs, for example after a power dip, the detector notices within 20 s and systemd restarts it. Old runs are deleted when the stick runs low.

## Installing the app

Download `tetra-alert.apk` from the latest release on the Releases page and install it. Android will ask you to allow installs from your browser or file manager.

The first time you open it:

1. Tap **Start detector link** and allow Bluetooth, notifications and location.
2. Set location to "Allow all the time", so alerts that arrive with the screen off still save where you were.
3. In Settings, enter the Pi's PIN and pair for auto-start. The app then starts by itself whenever the Pi is in range.
4. Set the app's battery use to Unrestricted: long-press the icon, then App info, then App battery usage.

### Android Auto

Android Auto only runs Car App Library apps that Android records as installed from Google Play, so a normal sideload never appears in the car. Two things fix that:

1. Turn on unknown sources in Android Auto. Open its settings, tap the version number about ten times to turn on developer mode, then in the developer settings turn on "Unknown sources".
2. Install the APK with [UnknownAuto](https://github.com/GitMag/UnknownAuto/wiki) instead of opening it directly. It uses [Shizuku](https://shizuku.rikka.app/) (set up once through Wireless debugging) to install the app marked as coming from Google Play. [KingInstaller](https://github.com/fcaronte/KingInstaller) does the same job. Install updates the same way, or Android Auto may drop the app again.

The car screen shows the status, the last alert, the strength meter and the last 10 minutes of alerts as bars. The top bar has a flag button for "I saw one" and a mute button. The big button underneath switches between Auto, City and Motorway.

## Using it

The Live tab shows the detector state, the Pi's temperature and load, the last alert and the last 10 minutes. It also has the City/Motorway switch, "Saw one", snooze and mute.

The History tab keeps the last 2,000 alerts with the location of each, and opens any of them in Google Maps. You can share the list as a CSV. Tap "Saw one" whenever you see a police car, ambulance or fire engine, beep or not. Those marks show up in History, and they are the only way to find out what the detector misses.

The Pi tab shows the Pi's health, turns its Wi-Fi back on, shuts it down, and downloads a run's `detections.csv` and `health.csv` over Bluetooth to share from the phone.

Settings holds the sound options, the voice, test sounds and the Tuning section.

City/Motorway mode decides whether medium alerts make a sound. In Auto it follows your GPS speed while you're in the car: City after a minute under 55 mph, Motorway after a minute over 55. You can change both speeds. STRONG alerts sound in either mode.

The app also mutes itself after 5 minutes parked and comes back once you're doing over 5 mph. A mute you set by hand clears when Android Auto disconnects, so it can't carry into the next drive.

A home-screen widget shows the same meter and bars.

### Tuning

Everything the phone decides about timing is in Settings, under Tuning:

| Setting | Default |
|---|---|
| Mute when parked for | 5 min |
| Repeat while STRONG | every 4 s |
| Say "Tetra clear" | after 20 s quiet |
| Medium alerts sound in | Motorway only |
| Snooze length | 30 s |
| City/Motorway switch delay | 1 min |
| Meter dims after | 30 s |
| Warn "detector offline" after | 15 s |
| Repeat offline warning | every 2 min, while Android Auto is connected |
| Give up when the Pi leaves | 5 min |

The detection settings themselves (strength bands, quiet gap, sensitivity) live in `CONFIG` at the top of `pi/live.py`. The plan is to make them adjustable from the phone too.

## Building from source

The app is Kotlin (minSdk 33, targetSdk 36) and builds with Gradle 8.14:

```
gradle :app:testDebugUnitTest :app:assembleDebug
```

Every push to `main` builds and tests the app on GitHub Actions and updates the `latest` release. Android only installs an update signed with the same key as the copy already on the phone, so the build signs with a fixed key kept in a repository secret. If you fork this, make your own debug keystore and add it, base64-encoded, as a secret called `SIGNING_KEY`. Local builds just use your own debug key.

The unit tests check that the byte formats in `Link.kt` match `pi/tetra_link.py`. If you change one side, change the other.

## What's in the repo

| Path | What it is |
|---|---|
| `app/` | The Android app, including Android Auto and the widget |
| `pi/live.py` | Detection, logging and health checks |
| `pi/ble_server.py`, `pi/tetra_link.py` | The Bluetooth link and its message formats |
| `pi/tetra_setup.sh`, `pi/install_services.sh`, `pi/tetra_power.sh` | Setup menu, systemd services and power saving |
| `pi/replay.py` | Re-runs saved logs through the current alert rules |
| `pi/detector.py`, `pi/survey.py`, `pi/bench.py`, ... | Earlier tools and test harnesses |
| `docs/techspec.md` | The full technical spec, with every protocol and known issue |

## Known limits

- With one dongle hopping across three windows, each part of the band is heard about a third of the time, so a very short transmission can be missed. Two dongles would fix that but need a Pi 4 or 5.
- The rules were tuned on a few hours of driving in one part of England. Other areas, other interference and other antennas may need different settings.
- The Bluetooth link isn't encrypted. Anyone nearby can see the alerts. Commands are protected only by the PIN and a one-time code.
- The detector can't tell a parked radio at a station from a vehicle driving towards you. Both just look strong.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
