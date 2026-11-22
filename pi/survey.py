#!/usr/bin/env python3
"""
survey.py - which TETRA carriers are active where you are?

Scans the Airwave downlink band (390-395 MHz) with the dongle, finds the 25 kHz
channels that carry a TETRA signal, converts each to its paired uplink channel
(downlink minus 10 MHz), and works out how many of them a single dongle window
could watch at once.

Needs gaintest.py in the same folder (it provides the dongle setup that
already works on your machine). Close any other SDR program first.

Run it in more than one place: you only hear the cells near you, and vehicles
attach to the cells near THEM, so a result at home is not the whole story.
"""

import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

import gaintest

CONFIG = {
    "sample_rate": 2_400_000,
    "gain_db": 32.8,              # safer than 36.4 close to masts
    "dwell_s": 20.0,              # per window
    "settle_s": 0.4,
    "fft_size": 2048,
    "usable_fraction": 0.80,
    "dc_exclude_hz": 100_000,
    "band_lo_hz": 390_000_000,
    "band_hi_hz": 395_000_000,
    "raster_base_hz": 390_012_500,
    "raster_step_hz": 25_000,
    "half_width_bins": 8,         # 17 bins is about 20 kHz
    "window_step_hz": 1_800_000,
    "pass_b_offset_hz": 800_000,  # second pass covers the first pass's DC spikes
    "steady_db": 6.0,             # median this far above floor = always on
    "burst_frac": 0.05,           # or on at least this share of the time...
    "burst_peak_db": 8.0,         # ...reaching this far above floor
    "narrow_top3": 0.60,          # more of the power in 3 bins = a spur
    "clip_level": 0.97,
    "max_clip_pct": 0.1,
    "backoff_db": 6.0,            # gain step down when clipping
    "skirt_db": 20.0,             # a neighbour this much stronger = skirt
    "skirt_reach": 3,             # channels either side
    "duplex_hz": 10_000_000,
    "window_width_hz": 1_920_000,  # what one dongle sees (80 % of 2.4 MHz)
}


def plan_windows():
    sr = CONFIG["sample_rate"]
    half = sr * CONFIG["usable_fraction"] / 2
    centres = []
    for offset in (0.0, CONFIG["pass_b_offset_hz"]):
        c = CONFIG["band_lo_hz"] + 900_000 + offset
        while c - half < CONFIG["band_hi_hz"]:
            centres.append(c)
            c += CONFIG["window_step_hz"]
    return centres


def window_channels(centre):
    """Channels this window can measure: (frequency, offset, bin indices)."""
    sr = CONFIG["sample_rate"]
    n = CONFIG["fft_size"]
    bin_hz = sr / n
    half = sr * CONFIG["usable_fraction"] / 2
    hw = CONFIG["half_width_bins"]
    out = []
    k = int(np.ceil((centre - half - CONFIG["raster_base_hz"])
                    / CONFIG["raster_step_hz"]))
    while True:
        f = CONFIG["raster_base_hz"] + k * CONFIG["raster_step_hz"]
        k += 1
        if f > centre + half:
            break
        if not (CONFIG["band_lo_hz"] <= f < CONFIG["band_hi_hz"]):
            continue
        off = f - centre
        if abs(off) < CONFIG["dc_exclude_hz"]:
            continue
        mid = n // 2 + int(round(off / bin_hz))
        idx = np.arange(mid - hw, mid + hw + 1)
        if idx[0] < 0 or idx[-1] >= n:
            continue
        out.append((f, off, idx))
    return out


def measure_window(sdr, centre, seconds):
    """Tune, dwell, and return per-channel statistics for this window."""
    sr = CONFIG["sample_rate"]
    n = CONFIG["fft_size"]
    chans = window_channels(centre)
    bin_matrix = np.array([c[2] for c in chans])
    win = np.hanning(n).astype(np.float32)

    sdr.center_freq = centre              # an error here closes the dongle
    time.sleep(CONFIG["settle_s"])
    sdr.read_samples(1 << 18)             # flush the retune transient

    need = int(seconds * sr)
    chunk = 1 << 20
    seen = clipped = 0
    frames = []
    mean_acc = np.zeros(n, dtype=np.float64)
    nframes = 0
    while seen < need:
        iq = np.asarray(sdr.read_samples(chunk), dtype=np.complex64)
        seen += len(iq)
        clipped += int(np.count_nonzero(
            (np.abs(iq.real) > CONFIG["clip_level"])
            | (np.abs(iq.imag) > CONFIG["clip_level"])))
        m = len(iq) // n
        spec = np.fft.fftshift(
            np.fft.fft(iq[:m * n].reshape(m, n) * win, axis=1), axes=1)
        power = np.abs(spec) ** 2
        mean_acc += power.sum(axis=0)
        nframes += m
        frames.append(power[:, bin_matrix].sum(axis=2).astype(np.float32))

    db = 10 * np.log10(np.concatenate(frames) + 1e-20)      # frames x channels
    mean_spec = mean_acc / nframes
    bin_floor = float(np.median(mean_spec))

    medians = np.median(db, axis=0)
    floor = float(np.median(medians))     # most channels are noise only
    results = []
    for j, (f, off, idx) in enumerate(chans):
        prof = np.clip(mean_spec[idx] - bin_floor, 0, None)
        tot = prof.sum()
        top3 = float(np.sort(prof)[-3:].sum() / tot) if tot > 0 else 0.0
        results.append({
            "freq_hz": f, "offset_hz": off, "window_centre_hz": centre,
            "floor_db": floor,
            "median_db": float(medians[j]),
            "p99_db": float(np.percentile(db[:, j], 99)),
            "occ": float(np.mean(db[:, j] > floor + CONFIG["steady_db"])),
            "top3": top3,
        })
    return results, 100.0 * clipped / seen


def classify(ch):
    excess = ch["median_db"] - ch["floor_db"]
    peak = ch["p99_db"] - ch["floor_db"]
    if excess >= CONFIG["steady_db"]:
        kind = "steady"
    elif ch["occ"] >= CONFIG["burst_frac"] and peak >= CONFIG["burst_peak_db"]:
        kind = "bursty"
    else:
        return "", excess
    if ch["top3"] > CONFIG["narrow_top3"]:
        kind += " (narrow: spur?)"
    return kind, excess


def best_window(uplinks):
    """Largest number of carriers one dongle window can hold."""
    if not uplinks:
        return 0, None, None
    f = sorted(uplinks)
    width = CONFIG["window_width_hz"]
    best = (0, f[0], f[0])
    j = 0
    for i in range(len(f)):
        while f[i] - f[j] > width:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, f[j], f[i])
    return best


def probe_clip(sdr, seconds=1.5):
    """Percent of samples at full scale over a short probe."""
    need = int(seconds * CONFIG["sample_rate"])
    seen = clipped = 0
    while seen < need:
        iq = np.asarray(sdr.read_samples(1 << 19), dtype=np.complex64)
        seen += len(iq)
        clipped += int(np.count_nonzero(
            (np.abs(iq.real) > CONFIG["clip_level"])
            | (np.abs(iq.imag) > CONFIG["clip_level"])))
    return 100.0 * clipped / seen


def choose_gain(sdr, centre):
    """
    Start at the configured gain and step down until the ADC stops clipping.
    A strong mast overloads the tuner; clipped data produces fake carriers on
    either side of the real one, so clipped measurements cannot be trusted.
    Returns (gain used, final clip %, log of attempts).
    """
    valid = sorted(g / 10.0 for g in sdr.gain_values)
    g = min(valid, key=lambda v: abs(v - CONFIG["gain_db"]))
    sdr.center_freq = centre
    time.sleep(CONFIG["settle_s"])
    log = []
    for _ in range(6):
        gaintest.set_gain_direct(sdr, g)
        sdr.read_samples(1 << 18)
        clip = probe_clip(sdr)
        log.append((g, clip))
        if clip <= CONFIG["max_clip_pct"]:
            return g, clip, log
        lower = [v for v in valid if v <= g - CONFIG["backoff_db"]]
        if not lower:
            break
        g = max(lower)
    return g, clip, log


def run_survey():
    label = input("\nLocation label (for the filename, e.g. home-roof, car-park): ")
    label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label.strip()) \
        or "unlabelled"
    centres = plan_windows()
    total = len(centres) * (CONFIG["dwell_s"] + CONFIG["settle_s"] + 1)
    print(f"\n{len(centres)} windows x {CONFIG['dwell_s']:.0f} s, gain "
          f"{CONFIG['gain_db']} dB. About {total/60:.1f} minutes. "
          "Ctrl+C aborts.")
    print("Antenna in place and SDR++ closed. Starting...\n")

    sdr = gaintest.open_sdr(CONFIG["gain_db"])
    if sdr is None:
        return

    per_freq = {}
    try:
        for i, centre in enumerate(centres, 1):
            print(f"  window {i}/{len(centres)}  centre {centre/1e6:.3f} MHz ...",
                  end=" ", flush=True)
            g, probe, log = choose_gain(sdr, centre)
            steps = " -> ".join(f"{a:.1f}dB:{b:.1f}%" for a, b in log)
            results, clip = measure_window(sdr, centre, CONFIG["dwell_s"])
            bad = clip > CONFIG["max_clip_pct"]
            for r in results:
                r["gain_db"], r["clipped"] = g, bad
            note = "  STILL CLIPPING, unreliable" if bad else ""
            print(f"gain {g:.1f} dB, clip {clip:.3f} %{note}")
            if len(log) > 1:
                print(f"      backed off: {steps}")
            for r in results:
                cur = per_freq.get(r["freq_hz"])
                # nearest the window centre = flattest part of the passband
                if cur is None or abs(r["offset_hz"]) < abs(cur["offset_hz"]):
                    per_freq[r["freq_hz"]] = r
    except KeyboardInterrupt:
        print("\nAborted.")
        return
    except Exception as exc:
        print(f"\nFailed: {exc}")
        print("pyrtlsdr closes the dongle after any failed call, so stopping.")
        return
    finally:
        try:
            sdr.close()
        except Exception:
            pass

    rows = []
    for f in sorted(per_freq):
        r = per_freq[f]
        kind, excess = classify(r)
        r["kind"], r["excess_db"] = kind, excess
        rows.append(r)
    reach = CONFIG["skirt_reach"]
    for i, r in enumerate(rows):
        if not r["kind"]:
            continue
        near = rows[max(0, i - reach):i] + rows[i + 1:i + 1 + reach]
        if any(n["excess_db"] >= r["excess_db"] + CONFIG["skirt_db"] for n in near):
            r["kind"] += " (skirt of a stronger carrier?)"
        if r.get("clipped"):
            r["kind"] += " [clipped window]"

    active = [r for r in rows if r["kind"]]
    real = [r for r in active
            if "narrow" not in r["kind"] and "skirt" not in r["kind"]
            and "clipped" not in r["kind"]]

    print(f"\nChannels measured: {len(rows)} of 200")
    print(f"Active: {len(active)}. Trustworthy carriers: {len(real)}. "
          f"Doubtful (spurs, skirts, clipped): {len(active) - len(real)}.\n")
    if active:
        print(f"{'downlink MHz':>13}  {'uplink MHz':>11}  {'above floor':>11}  "
              f"{'on-time':>7}  {'gain':>5}  kind")
        for r in active:
            up = (r["freq_hz"] - CONFIG["duplex_hz"]) / 1e6
            print(f"{r['freq_hz']/1e6:13.4f}  {up:11.4f}  "
                  f"{r['excess_db']:9.1f}dB  {100*r['occ']:6.0f}%  "
                  f"{r['gain_db']:5.1f}  {r['kind']}")
    top = sorted(real, key=lambda r: -r["excess_db"])[:3]
    if top:
        print("\nStrongest here (probably the nearest mast): " + ", ".join(
            f"{r['freq_hz']/1e6:.4f} MHz (+{r['excess_db']:.0f} dB)" for r in top))

    ups = [r["freq_hz"] - CONFIG["duplex_hz"] for r in real]
    print()
    if not real:
        print("No TETRA carriers found. Check the antenna, the gain, and that "
              "SDR++ is closed.")
    else:
        k, lo, hi = best_window(ups)
        lower = sum(1 for u in ups if u < 382_500_000)
        print(f"Carriers heard here: {len(real)}")
        print(f"Best single dongle window ({CONFIG['window_width_hz']/1e6:.2f} MHz "
              f"wide) holds {k} of {len(real)} ({100*k/len(real):.0f} %): "
              f"uplink {lo/1e6:.4f} to {hi/1e6:.4f} MHz.")
        print(f"Split at 382.5 MHz: {lower} in the lower half, "
              f"{len(real) - lower} in the upper half.")
        if k == len(real):
            print("One dongle covers every carrier heard here.")
        else:
            print(f"One dongle would miss {len(real) - k} carrier(s) here. "
                  "Hopping or a second dongle would cover them.")
        print("\nThis is only the cells you can hear from this spot. Repeat "
              "elsewhere before deciding.")

    path = Path(f"survey_{label}_{datetime.now():%Y%m%d_%H%M%S}.csv")
    cols = ["freq_hz", "offset_hz", "window_centre_hz", "floor_db", "median_db",
            "p99_db", "occ", "top3", "excess_db", "gain_db", "clipped", "kind"]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r[c] for c in cols})
    print(f"\nSaved {path.resolve()}")
    print("Send me this file or paste the table above.")


def edit_settings():
    print(f"\nGain {CONFIG['gain_db']} dB, dwell {CONFIG['dwell_s']:.0f} s per window.")
    raw = input("New gain (blank keeps): ").strip()
    if raw:
        try:
            CONFIG["gain_db"] = float(raw)
        except ValueError:
            print("Could not read that. Unchanged.")
    raw = input("New dwell seconds (blank keeps): ").strip()
    if raw:
        try:
            CONFIG["dwell_s"] = max(3.0, float(raw))
        except ValueError:
            print("Could not read that. Unchanged.")


def main():
    while True:
        print("\n=== TETRA downlink survey ===")
        print("1. Check the dongle can be opened")
        print("2. Run the survey")
        print("3. Change gain / dwell time")
        print("4. Quit")
        choice = input("> ").strip()
        if choice == "1":
            gaintest.check_environment()
        elif choice == "2":
            run_survey()
        elif choice == "3":
            edit_settings()
        elif choice == "4":
            return


if __name__ == "__main__":
    main()
