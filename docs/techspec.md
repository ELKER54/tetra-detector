# TETRA Detector - Technical Specification

Android app 0.26 (versionCode 29). App at the repo root, Pi code in `pi/`, this spec in `docs/`.
Status: working end to end in one car with wireless Android Auto; tested on a few hours of driving.

---

## 0. Brief for the reviewer

You are reviewing a DIY receive-only detector for UK Airwave (TETRA) uplink activity, i.e. transmissions from nearby police / ambulance / fire handsets and vehicle radios. It detects **energy and timing only**. It does not demodulate, decode or decrypt anything, and must stay that way.

Please look for:

1. **Things that can fail silently on a long drive**: anything that stops alerts without the driver knowing.
2. **Logic or protocol bugs** between the Pi (Python) and the phone (Kotlin): byte layouts, nonces, sequencing, timeouts.
3. **Android platform pitfalls**: foreground service rules, background location, companion device APIs, Android Auto template limits, Android 15/16 behaviour.
4. **Power, heat and storage risks** on a Pi 3B in a car.
5. **Security holes** in the PIN-protected commands (shutdown, Wi-Fi on, log download).
6. **Anything in "Known issues" (section 12) that is worse than stated, plus anything missing from it.**

Section 13 lists what has and hasn't been tested on real hardware.

---

## 1. System overview

```
 RTL-SDR Blog V3  --USB-->  Raspberry Pi 3B  --BLE GATT-->  Android phone --wireless-->  Android Auto
 (380-385 MHz)             live.py (detect)                TETRA Alert app            Pane template
                           ble_server.py (BLE)             (foreground service)       (meter + bars image)
                           USB stick logs                  alert history + GPS
```

| Part | Detail |
|---|---|
| SDR | RTL-SDR Blog V3, ~270-280 mA |
| Computer | Raspberry Pi 3B, Pi OS Trixie Lite 64-bit, Python 3.13 in venv `~/sdrenv` |
| Antenna | Co-Star Midi Mag kit MD-TET-5F (magnetic whip, 380-430 MHz, 5 m RG58, FME) with an FME to SMA adaptor |
| Storage | USB stick, ext4, label `TETRADATA`, mounted `/mnt/tetra` (`nofail,commit=5`) |
| Phone | Android 16 (minSdk 33) |
| Car | Any car with Android Auto (tested wireless) |
| Power (current) | Car USB-C port, then a USB-C female to micro-USB adapter (rated 2 A). Has shown under-voltage (`get_throttled` 0x50005). 12 V supply not yet fitted. |
| Cooling | Fan case. Without one, 63-72 °C was seen in a car; with one, 37 °C at idle. |

Measured draw: Pi idle 260 mA, Pi flat out 730 mA, dongle 270-280 mA, total about 0.9-1.0 A.

---

## 2. Radio and detection (live.py)

### 2.1 Tuning
- Uplink band 380-385 MHz, covered by **hopping between 3 windows** centred 380.850 / 382.500 / 384.150 MHz.
- Sample rate 2.4 MS/s, dwell 0.8 s per window.
- Each dwell is read in **one call** as raw uint8 I/Q, converted with a lookup table to complex64.
- `settle_samples = 1<<17` (~55 ms) are discarded after each retune; work is done in chunks of `1<<18` (~109 ms).
- Starting gain 32.8 dB. If more than 0.1 % of samples clip, gain drops by 3 dB. It steps back up after 30 s clean, and the wait doubles (up to 300 s) each time a raise overloads again.

### 2.2 Detection chain
1. FFT 2048 (scipy if present, numpy otherwise).
2. **CFAR** per bin, offset 5 dB above local noise.
3. **Bandwidth gate**: about 25 kHz wide, roughly one TETRA channel.
4. **Duration classes**: bursts matched to 5.722 ms or 12.833 ms.
5. Burst tracker, which merges hits into detections per channel.
6. **Vetoes / statuses** (Alerter):
   - `beep`: real new activity, alert sent.
   - `active`: ongoing episode, no new beep.
   - `quiet`: inside the quiet zone (383.85-384.20 MHz, a repeating false source) **and** not frame-locked. A locked channel in the zone alerts normally.
   - `wideband`: 3 or more channels in one dwell, so it's treated as interference.
   - `unlocked`: the channel's gaps don't fit the TETRA 56.667 ms frame grid. That means fewer than 30 % fit, within ±4 ms, over at least 4 gaps in the last 120 s. Tuning data: real channels 84 % fit, interference 5-12 %.
7. **Episodes** per channel: `[last t, max dB, max tone]`. A beep is sent:
   - on new activity after a 30 s quiet gap;
   - when the tone band rises (weak → medium → STRONG);
   - on +10 dB within an episode.

   A backward clock jump of more than 5 s starts a new episode.
8. **Tone bands** (dB above noise): weak < 15, medium 15-30, STRONG > 30.

### 2.3 Performance
- A multiprocessing pool, one worker per core (4 on the Pi 3B), runs the order-independent maths (`chunk_maths`). The main process runs `apply_chunk`, the order-dependent veto and tracker.
- Queue of 6 dwells (~4 MB each). If processing is more than 1.0 s behind at the start of a dwell, that dwell is skipped, so beeps are never stale.
- Seen busy at 68-72 % with 4 workers, previously 97-100 % single-core.

### 2.4 Outputs per run (folder `live_<tag>_<YYYYMMDD>_<HHMMSS>`)
- `detections.csv`: time, freq MHz, class, duration ms, dB, window centre, gain, snippet, status, frame fit.
- `health.csv`: time, temp_c, throttled, busy_pct, lag_s, skipped, free_mb, note. The note records clock jumps, detected from the wall clock against the monotonic clock.
- `activity.csv` (if written).
- Snippets: raw I/Q around bursts, at most 150, ~2 MB each, 0.2 s padding. Not saved below 500 MB free.
- Writer: `Store` class. CSVs are fsynced every 30 s. If the stick fails or fills, logging stops but **alerts keep working**, and status flag bit4 is set.
- On a headless start, the oldest run folders are deleted until 2000 MB is free.
- A power cut can leave NUL bytes at the end of a CSV. `replay.py` and the log zipper strip them.

### 2.5 Headless mode
`live.py --headless car`, under systemd:
- Writes to `/mnt/tetra/runs`, or falls back to `~/runs` with a warning.
- Warns if `/mnt/tetra` turns out to be an overlay (i.e. RAM).
- Sends status to ble_server every 5 s.
- Samples temp / throttled / busy every `status_every_s` = **30 s**. Until the first sample, temp and busy are reported as unknown, which the BLE layer sends as 0.

---

## 3. Pi to phone link

### 3.1 Local hop: live.py to ble_server.py
UDP JSON to 127.0.0.1:47474 (`tetra_link.Notifier`):
- `{"type":"status","state":..,"temp_c":..,"busy_pct":..,"flags_now":..}`
- `{"type":"alert","tone":0-2,"db":float,"freq_hz":int}`

### 3.2 BLE GATT (ble_server.py, bless 0.3.0 over BlueZ D-Bus, runs as root)
Device name `TDET`. Service `d11b6b94-3a64-46b5-8721-7f564f0815a0`. All values little-endian.

| Char | UUID suffix | Props | Layout |
|---|---|---|---|
| ALERT | `...95` | notify | u16 seq, u8 tone, i16 db×10, u32 freq_hz (9 B) |
| STATUS | `...96` | notify | u16 seq, u8 state, u8 temp_c, u8 busy_pct, u8 flags, u32 nonce (10 B) |
| CONTROL | `...97` | write | `SD` + HMAC8, or `WI` + HMAC8 (10 B) |
| TIME | `...98` | write | u64 epoch ms (8 B) |
| FILES | `...99` | write + notify | request and framed reply, see 3.4 |
| LIVE | `...9a` | notify | u8 tone, i16 db×10, u32 freq_hz (7 B): current strength of a busy channel, at most every 2 s per channel; never a beep by itself |

- **States**: 0 not running, 1 running, 2 dongle error, 3 stalled.
  - The Pi reports **stalled** if there has been no status from live.py for 15 s.
  - It reports **not running** if live.py has never been heard from since ble_server started.
- **Flags**:
  - bit0 under-voltage now, bit1 CPU capped, bit2 throttled, bit3 temp limit (all from `vcgencmd get_throttled`);
  - bit4 storage failed or full;
  - bit5 Pi Wi-Fi radio on (from rfkill);
  - bit6 Pi falling behind (dwells skipped in the last 30 s).
- **Heartbeat**: STATUS every 3 s.

### 3.3 PIN-protected commands
- PIN stored in `/etc/tetra/pin` on the Pi and in the app's prefs. With no PIN file, the commands are disabled.
- MAC = first 8 bytes of HMAC-SHA256(PIN, …):
  - shutdown `SD`: message = nonce (u32 LE);
  - Wi-Fi on `WI`: message = `"WI"` + nonce;
  - files: message = cmd + arg (UTF-8) + nonce.
- The nonce lives in STATUS and **changes after every attempt**, valid or not, so a recorded request can't be replayed.
- Rate limits:
  - CONTROL: one attempt per 5 s.
  - FILES: only failures block, for 5 s, so a list straight after a download isn't slowed.
- The Pi sends a fresh STATUS after each FILES request so the phone has the new nonce.
- The nonce is **shared** between CONTROL and FILES.

### 3.4 Log download (FILES)
- Request: cmd (1 B) + MAC (8 B) + arg.
  - `L`: list the runs, newest first, max 30, as `[{name, bytes}]`. Folders with no log files are skipped.
  - `G` + run name: one run's logs as a zip. The name must match `live_[A-Za-z0-9_-]{1,80}`. The zip holds detections, health and activity CSVs, with NULs stripped, max 20 MB.
- Reply: header `H` + kind (`L`/`G`/`E`) + u32 length + u32 CRC-32, then `D` + up to 180 B frames, paced 8 ms apart. One send at a time; if busy, it replies `E` "Busy".
- The phone:
  - requests MTU 247 and refuses downloads if MTU < 184;
  - reassembles the frames and checks the CRC;
  - gives up after 20 s with no frame;
  - unzips to cache as `<run>_detections.csv` etc., renaming each file from a cleaned version of its name rather than trusting zip paths;
  - shares them via FileProvider (`ACTION_SEND` / `ACTION_SEND_MULTIPLE`, `text/csv`).
- Test results: a 46,864-byte zip from a 464 KB detections file arrived intact, 262 frames, CRC ok (simulated BlueZ only).

### 3.5 Time sync
- The Pi 3B has no clock battery and no internet in the car.
- On every connect the phone writes its time to TIME. The Pi runs `date -u -s @t` if its clock is off by more than 2 s and the time is between 2026 and 2100.

### 3.6 No BLE encryption
There's no pairing or bonding, because fixed-passkey pairing isn't possible on a headless Pi with bless. **Anyone in range can subscribe to ALERT and STATUS.** The commands are protected only by the HMAC and nonce. The owner accepted this.

---

## 4. Pi system configuration

### 4.1 systemd services (`install_services.sh`)
| Service | User | Command | Restart |
|---|---|---|---|
| `tetra-ble` | root | `ble_server.py` | always, 10 s; requires bluetooth.service |
| `tetra-live` | the installing user | `live.py --headless car` | always, 10 s; after tetra-ble and mnt-tetra.mount |
| `tetra-power` | root | `tetra_power.sh` (Type=simple) | none |

`tetra_power.sh`:
- turns the LEDs off;
- tries to turn HDMI off with `vcgencmd display_power 0` (may do nothing under KMS);
- runs `rfkill unblock wifi`, waits up to 300 s for wlan0 to get an IPv4 address, and blocks Wi-Fi if it doesn't get one. The phone app can turn Wi-Fi back on (`WI` command).

### 4.2 Read-only root (`tetra_setup.sh` menu)
Menu:
1. Prepare USB stick (wipe, needs typed `WIPE`)
2. Set PIN
3. Install services
4. Protect
5. Unprotect
6. Status
7. Switch off unneeded services
8. Quit

- **Protect** runs `raspi-config do_overlayfs 0`, then patches cmdline with `overlayroot=tmpfs:recurse=0`, so `/mnt/tetra` stays writable on the real stick and not in RAM.
- **Unprotect** reverses the patch, then runs `do_overlayfs 1`.
- **Switched off by option 7**:
  - timers: man-db, apt-daily, apt-daily-upgrade, dpkg-db-backup, e2scrub_all, fstrim, rpi-zram-writeback;
  - avahi service and socket;
  - getty@tty1.
- Updating Pi code: Unprotect (reboot), scp the files, menu 3, then Protect (reboot).

### 4.3 Known library quirk
Debian's librtlsdr lacks `rtlsdr_set_dithering`. pyrtlsdr is patched through a stub in `gaintest.py`, which `live.py` imports first.

---

## 5. Android app

### 5.1 Build
- Kotlin 2.2.0, AGP 8.13.2, compileSdk / targetSdk 36, minSdk 33.
- Dependencies: car-app 1.7.0, androidx.core 1.7.0 (for FileProvider), junit 4.13.2.
- Sideloaded **debug build**. Android Auto runs it via "Unknown sources" in AA developer settings. Host validator: allow all hosts when debuggable.
- CI: GitHub Actions builds, runs unit tests, and updates the `latest` release in place with `tetra-alert.apk`, signed with the key in the `SIGNING_KEY` secret.
- **`debug.keystore` is committed to the repo.**

### 5.2 Components
| Class | Role |
|---|---|
| `DetectorService` | Foreground service (`connectedDevice`). Scan with service filter, connect, MTU 247, discover, subscribe ALERT, then STATUS, then FILES (if present), write TIME. Watchdog, reconnect, log download API, GATT write retry (12× 250 ms). |
| `DetectorState` | Global state + prefs (muted, media route, weak sound, PIN), `summary()`, `piStats()` (hides temp/load while 0), `piWarnings()`. |
| `Link` | UUIDs, parse/pack, HMAC tokens, `filesRequest`, `Reassembler` (H/D frames, CRC32). |
| `Tones` | Audio: TTS "Tetra" then a tone (pips by band); "Tetra active" once per protection episode; offline warning. Navigation-guidance audio route (default) or media. |
| `PiPresence` | Companion Device Manager association with the Pi; presence observing (`ObservingDevicePresenceRequest` on Android 16) to auto-start the link when the Pi is in range. |
| `AlertLog` | Alert history with GPS, see 5.4. |
| `MainActivity` | "Signal" dark UI: Live / History / Pi / Settings tabs. |
| `AlertCarAppService` / `StatusScreen` / `CarPicture` | Android Auto, see 5.5. |
| `TetraWidget` | Home-screen widget, see 5.6. |
| `UpdateReceiver` | `MY_PACKAGE_REPLACED`: restarts the link after an app update if paired. |
| `CrashLog` | Saves the last uncaught crash and shows it on next launch. |

### 5.3 Link behaviour
- No good heartbeat for 15 s: shows offline and plays a warning tone and announcement, repeated every 2 min while Android Auto is connected. If Android reports the Pi has left range, the link stops after 5 min.
- Rescans are spaced at least 6 s apart, because Android limits how often scans can start.
- **Repeat while STRONG**: STRONG pips (no voice) every 4 s while a STRONG alert or LIVE update is under 4.5 s old. On by default.
- **"Tetra clear"**: said once after a STRONG episode when nothing above weak has been heard for 20 s (setting: 20 / 30 / 60 s / off). Replay of the saved drives: about 5 per hour on the move, but up to about 1 a minute when parked near busy radio traffic at 20 s; 60 s cuts that to about 1 per drive.
- **Tuning** (Settings): mute when parked (default 5 min under 3 mph with Android Auto on; ends above 5 mph), repeat-while-STRONG interval, "Tetra clear" delay, when medium sounds, snooze length, City/Motorway switch delay, meter dim time, offline warning delay and repeat, give-up time. Reset to defaults.
- **Snooze** (phone only, length in Tuning) silences alert sounds; **mute switches itself off when Android Auto disconnects**.
- **Weak alerts are hidden** by default (setting: Hidden / Shown, silent / Shown with sound). Hidden means the phone ignores them completely; the Pi still logs them.
- **City / Motorway**: STRONG always sounds. Medium sounds only in Motorway. Auto mode starts in City and uses GPS speed while Android Auto is connected; no speed reading for 30 s counts as slow. City after 60 s below the City threshold, Motorway after 60 s above the Motorway threshold (both set in Settings, default 55 mph each; if set apart, no change in between). Override on the Live tab, in Settings and as a button on the car screen: Auto / City / Motorway. The mode shows on the Status line.
- **Mute** silences all alert sounds. Alerts are still logged.
- **Auto-start**:
  - CDM presence → `PiPresenceService` → `DetectorService.start`;
  - also on app update;
  - also when the car screen opens with the link off and the Pi paired.

  The app holds `REQUEST_COMPANION_START_FOREGROUND_SERVICES_FROM_BACKGROUND`.

### 5.4 Alert history and location
- Every alert, weak included, is stored in `filesDir/alerts.txt`, one CSV line each: `ms,tone,db,freq_hz,lat,lon,acc_m`.
- Keeps the newest **2000** entries, rewriting the file when it passes 2200.
- Location: uses the newest last-known fix (fused, then GPS, then network) if it's under 15 s old. Otherwise it asks for the current location and saves whatever it has after 12 s. Saving never delays the sound.
- Permissions: FINE + COARSE, then BACKGROUND ("Allow all the time"), requested from the app.
- **Sightings**: a "Saw one" button (phone Live tab, flag icon in the car header) saves the time and location with tone -1, shown as SAW ONE in History and the CSV, ignored by the meter and bars. Double taps within 3 s are ignored. Used to measure what the detector catches against what the driver saw.
- The History tab is a ListView, newest first, with a strength badge, time, dB, MHz and accuracy. **Map** opens `geo:lat,lon?q=lat,lon(label)`. Export as CSV (share sheet) and Clear (with a confirm dialog).

### 5.5 Android Auto (category IOT)
- **Car API ≥ 4: `PaneTemplate`.**
  - Header: app icon, "TETRA Alert", a flag icon ("Saw one") and a mute/unmute **speaker icon** (end header action, drawn to a bitmap because the vector resource showed as a white square).
  - 1 row, **Status**, with two lines: state, temp/load once known, mode, warnings and MUTED; then the last alert on one line.
  - One body button: City / Motorway mode (Auto → City → Motorway).
  - Image: `CarPicture`, 384×384 RGB_565 (~290 KB): a 9-segment meter (6 dB per segment, dimmed after 30 s) and the last 10 minutes in 20 × 30 s bars.
- **Car API < 4: `ListTemplate` fallback**, fixed rows: Status, Last alert, Mute toggle, Detector link, Test alert sound.
- **Template quota**: Android Auto allows 5 templates per task. An update is a free "refresh" only if the header title and **every row title and the row count** stay the same, so all live text goes in the row text lines. Invalidates are batched to at most 1 per second, plus a 10 s tick.
- **Seen in the car**: this layout fits without scrolling on the test car's screen. Wider images get cropped to square, so the picture stays square.

### 5.6 Widget
- RemoteViews: a 96 dp picture (same `CarPicture`), status line, big dB + strength, time + MHz. `updatePeriodMillis=0`, so it's only updated by the service.
- Updates straight away when the connection state, an alert or a warning changes. Otherwise at most once every 60 s, plus one redraw when an alert turns 30 s old.
- Shows "Link off" when the service stops, and stays like that until the link is back.

### 5.7 Unit tests (8, all pass)
alert parse, status parse, shutdownToken, packTime, wifiOnToken, filesRequest (the vector `fbb19d47aad8d7f7` for PIN 1234 / G / live_car_1 / nonce 0x01020304), Reassembler (good and corrupted), history line round trip.

---

## 6. Data retention summary

| Where | What | Limit |
|---|---|---|
| Pi USB stick | Per-run CSVs + snippets | Oldest runs pruned on headless start until 2 GB free; snippets stop under 500 MB |
| Phone | Alert history | Last 2000 alerts (about 1.5 days at the home rate of ~1 weak alert/min) |
| Phone cache | Downloaded CSVs for sharing | Overwritten per download, cleared by Android when space is needed |

---

## 7. Power and heat plan
- Current: the car USB-C chain has shown under-voltage. The Monday drive's `health.csv` will show whether it's bad enough to matter.
- Planned: a proper 12 V to 5 V 3 A supply (not bought), and the fan case (arriving).
- The Pi 3B slows itself at 80 °C (soft) and 85 °C (hard). Seen 72 °C parked with no cooling.
- Savings already in place: LEDs off, HDMI off (best effort), Wi-Fi off when away from home, unneeded services off.

---

## 8. Validation so far
- **Detected for real**: an ambulance with blue lights on, and several channels that were busy most of the time in the test area. Frame-timing classification separates real channels from interference.
- **Unidentified**: a 380.0125 MHz interference source (likely own kit), rejected as `unlocked`.
- **Beep test**: navigation and media routes heard on Android Auto, focus granted, music ducks and comes back.
- **Missed**: one sighted vehicle produced no beep. Replay showed weak, frame-locked bursts inside the quiet zone, which led to the quiet-zone change.

---

## 9. Deployment checklist (current versions)
1. Pi: Unprotect, then scp `ble_server.py` and `tetra_link.py` (FILES support), then menu 3, then Protect.
2. Phone: install the latest `tetra-alert.apk`. Set the PIN in Settings to match `/etc/tetra/pin`, allow location "all the time", and pair auto-start.
3. Fit the fan case. Check that `vcgencmd get_throttled` reads `0x0` after 10 minutes, and the temperature.

---

## 10. Planned / backlog
- **After Monday**: a "full picture" Android Auto screen using `MapWithContentTemplate` + `ACCESS_SURFACE` / `MAP_TEMPLATES`, as a Settings switch defaulting to the current screen. It's unconfirmed whether Android Auto accepts it from an IOT app.
- A 12 V supply and a decision on where the Pi lives (boot or cabin).
- A motorbike version (power, weather, audio path all different).
- History limit: maybe raise to 20,000, or don't store weak alerts.

---

## 11. Explicitly out of scope
- Demodulating, decoding or decrypting TETRA content (illegal under the Wireless Telegraphy Act and not wanted).
- BLE encryption (dropped).
- Android Auto heads-up pop-ups (owner declined).

---

## 12. Known issues and open risks

| # | Issue | Impact | Status |
|---|---|---|---|
| 1 | Under-voltage on the car USB-C chain | Throttling, SD/USB corruption risk, missed dwells | Monday's health.csv will show; 12 V supply not bought |
| 2 | Heat, 72 °C seen with no cooling | Throttle at 80 °C | Fan case arriving |
| 3 | First 30 s after start, temp/load unknown (sent as 0) | Cosmetic; the app now hides it | Fixed in the app; the Pi still sends 0. Pi could send a reading straight away |
| 4 | No BLE encryption | Anyone nearby can see alerts/status | Accepted |
| 5 | Shared nonce between CONTROL and FILES | A failed CONTROL attempt invalidates a FILES request in flight and vice versa | Probably harmless (phone reads the new nonce from STATUS); unverified |
| 6 | FILES pacing (8 ms/frame) untested on real BlueZ | Dropped notifications show as "damaged" / "stopped sending" | Test pending |
| 7 | A long run's zip over BLE may take ~40-60 s | User must keep the app open | Accepted |
| 8 | Phone history capped at 2000 alerts | Old drives drop off if left running at home for days | Option to raise |
| 9 | Pi code was not in git | Loss risk | Fixed: now in `pi/`. Copies on the Pi may differ if edited there |
| 10 | `debug.keystore` committed to the repo | Only matters if the repo is ever made public | Repo is private; accepted |
| 11 | Android Auto "x s ago" relies on 10 s refreshes | Hosts may throttle refreshes; text could lag | Seen working briefly |
| 12 | Widget only updates while the link service runs | Stale "Link off" until the next start | Accepted |
| 13 | Auto-start when the car screen opens overrides a manual stop | Surprise restart | Accepted by design |
| 14 | `vcgencmd display_power 0` may do nothing under KMS | Tiny power waste | Best effort |
| 15 | Quiet zone 383.85-384.20 MHz | Was hiding real radios (weak, frame-locked bursts seen on replay) | Fixed: zone only mutes channels without TETRA timing |
| 16 | Alert time is when the phone received it, not when the Pi detected it | Offset of BLE latency (normally under 1 s) | Accepted |
| 17 | A sighted vehicle was not detected | Likely the quiet zone: weak (+6-8 dB) locked bursts on 384.0125 | Zone fixed; weak alerts are still silent by default |
| 19 | Hung dongle read never returns | Detector dead until power cycle | Fixed: no data for 20 s, so the run exits and systemd restarts it |
| 20 | Offline warning played once only | Easy to miss over music | Fixed: repeats every 2 min while Android Auto is connected |
| 18 | The debug build is sideloaded; Android Auto "Unknown sources" must stay on | An AA update could reset it and hide the app | Watch for it |

---

## 13. Tested vs untested

| Feature | Real hardware | Simulated / unit only |
|---|---|---|
| Detection + alerts + beeps | ✅ several car drives | |
| BLE ALERT/STATUS/CONTROL/TIME | ✅ | |
| Auto-start via CDM | ✅ | |
| Android Auto Pane + image + mute icon | ✅ parked | |
| Link restart after update | ✅ | |
| FILES log download | ❌ | ✅ fake BlueZ, unit tests |
| History locations with the screen off | ❌ | |
| Widget | ❌ | |
| 4-hour continuous run | ❌ (Monday) | |
| Pi on the fan case | ❌ (Sunday) | |
