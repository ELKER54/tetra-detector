#!/usr/bin/env python3
"""
gaintest.py - measure how much the antenna lifts the noise floor at each gain

Drives the RTL-SDR directly (close SDR++ first). For each gain it captures a
few seconds with the antenna plugged in and a few seconds with it unplugged,
and reports the difference. The bigger the gap, the more the signal chain is
limited by what the antenna hears rather than by the dongle's own noise.

Setup (once, Windows):
    pip install pyrtlsdr numpy
    rtlsdr.dll and libusb-1.0.dll must be findable. Easiest: copy both next
    to this script. SDR++ ships them in its own folder (look for rtlsdr.dll).
"""

import csv
import os
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent

# Python 3.8+ on Windows no longer searches the script folder or cwd for DLLs,
# so the folder has to be registered explicitly before pyrtlsdr is imported.
os.environ["PATH"] = str(HERE) + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory"):
    try:
        os.add_dll_directory(str(HERE))
    except OSError:
        pass


STUBBED = []


def _install_missing_symbol_stub():
    """
    pyrtlsdr 0.5+ binds optional functions (PLL dithering, tuner bandwidth...)
    that older librtlsdr builds do not export, and fails at import. Replace a
    missing rtlsdr_* function with a stub: success for dithering (not needed
    here), an error code for anything else, so a real call fails loudly.
    Core functions (open, rate, frequency, gain, read) exist in every build.
    """
    import ctypes
    original = ctypes.CDLL.__getattr__

    def patched(self, name):
        try:
            return original(self, name)
        except AttributeError:
            if not name.startswith("rtlsdr_"):
                raise
            ret = 0 if name == "rtlsdr_set_dithering" else -1

            def stub(*args, _ret=ret):
                return _ret
            STUBBED.append(name)
            return stub

    ctypes.CDLL.__getattr__ = patched


_install_missing_symbol_stub()


def pe_bits(path):
    """32 or 64 for a Windows DLL, None if it can't be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        off = struct.unpack_from("<I", head, 0x3C)[0]
        if head[off:off + 4] != b"PE\0\0":
            return None
        machine = struct.unpack_from("<H", head, off + 4)[0]
        return {0x14C: 32, 0x8664: 64, 0xAA64: 64}.get(machine)
    except (OSError, struct.error):
        return None


def diagnose_dlls():
    py_bits = 64 if sys.maxsize > 2 ** 32 else 32
    print(f"\nThis Python is {py_bits}-bit. DLLs next to the script:")
    for name in ("rtlsdr.dll", "libusb-1.0.dll"):
        p = HERE / name
        if not p.exists():
            print(f"  {name}: MISSING")
            continue
        b = pe_bits(p)
        verdict = "unreadable" if b is None else f"{b}-bit"
        flag = "  <-- WRONG, must match Python" if b and b != py_bits else ""
        print(f"  {name}: {verdict}{flag}")

CONFIG = {
    "centre_hz": 382_012_500,
    "sample_rate": 2_400_000,
    "gains_db": [32.8, 36.4, 38.6, 40.2],
    "seconds": 5.0,
    "settle_s": 0.6,                  # discarded after each gain change
    "fft_size": 2048,
    "channel_hz": 25_000,
    "usable_fraction": 0.80,
    "dc_exclude_hz": 100_000,         # keep clear of the LO spike
    "line_lift_db": 10.0,             # narrow line: this far above local median
    "line_median_bins": 101,
    "clip_level": 0.97,               # |I| or |Q| above this counts as clipped
    "target_gap_db": 9.0,
    "max_clip_pct": 0.1,
}


# ---------------------------------------------------------------- hardware


def _lib():
    # Depending on the pyrtlsdr version, this name is either the loaded DLL
    # itself or a module that holds it.
    from rtlsdr import librtlsdr as m
    return m if hasattr(m, "rtlsdr_open") or not hasattr(m, "librtlsdr") \
        else m.librtlsdr


def set_gain_direct(sdr, gain_db):
    """
    Set a manual gain through pyrtlsdr and return what the tuner reports.
    (Name kept from an earlier version; it now just uses the normal setter.)
    pyrtlsdr closes the device on ANY failed call, so errors must never be
    swallowed after a call: everything after would hit a closed device.
    """
    sdr.gain = gain_db
    return float(sdr.gain)


def open_sdr(gain_db):
    try:
        from rtlsdr import RtlSdr
    except Exception as exc:
        print("\nCould not load the RTL-SDR library:")
        print(f"  {exc}")
        diagnose_dlls()
        print("\nBoth DLLs must be in the same folder as this script and match")
        print("Python's bit-width. Tell me what the lines above say.")
        return None
    try:
        sdr = RtlSdr()
    except Exception as exc:
        print("\nCould not open the dongle:")
        print(f"  {exc}")
        print("\nIs SDR++ (or anything else) still running? Close it. Is the")
        print("Zadig WinUSB driver still installed on the dongle?")
        return None

    # No freq_correction call: the default is already 0 ppm, and setting 0 again
    # makes librtlsdr return an error, which makes pyrtlsdr close the device.
    step = "setup"
    try:
        step = "sample rate"
        sdr.sample_rate = CONFIG["sample_rate"]
        step = "centre frequency"
        sdr.center_freq = CONFIG["centre_hz"]
        step = "RTL AGC off"
        sdr.set_agc_mode(False)
        step = "gain"
        set_gain_direct(sdr, gain_db)
    except Exception as exc:
        print(f"\nSetup failed at '{step}': {exc}")
        print("pyrtlsdr closes the dongle after any failed call, so stopping here.")
        return None
    return sdr


def capture_stats(sdr, gain_db):
    """
    Set gain, wait for it to settle, then stream for `seconds`, accumulating
    the average power spectrum and clipping statistics without keeping the
    raw samples (5 s at 2.4 MS/s would be ~100 MB of complex128).
    """
    n = CONFIG["fft_size"]
    actual = set_gain_direct(sdr, gain_db)
    sdr.read_samples(1 << 18)                 # flush stale buffer
    time.sleep(CONFIG["settle_s"])
    sdr.read_samples(1 << 18)

    total_needed = int(CONFIG["seconds"] * CONFIG["sample_rate"])
    chunk = 1 << 20
    win = np.hanning(n).astype(np.float32)

    acc = np.zeros(n, dtype=np.float64)
    frames = 0
    clipped = 0
    seen = 0
    while seen < total_needed:
        iq = np.asarray(sdr.read_samples(chunk), dtype=np.complex64)
        seen += len(iq)
        clipped += int(np.count_nonzero(
            (np.abs(iq.real) > CONFIG["clip_level"]) |
            (np.abs(iq.imag) > CONFIG["clip_level"])))
        m = len(iq) // n
        batch = iq[:m * n].reshape(m, n)
        spec = np.fft.fft(batch * win, axis=1)
        acc += (np.abs(spec) ** 2).sum(axis=0)
        frames += m

    spectrum = np.fft.fftshift(acc / frames)  # mean power per bin, DC centred
    return {
        "gain_set": gain_db,
        "gain_actual": float(actual),
        "spectrum": spectrum,
        "clip_pct": 100.0 * clipped / seen,
    }


# ---------------------------------------------------------------- analysis

def analyse_spectrum(spectrum):
    """
    Returns median 25 kHz channel power in dB (arbitrary reference, only
    differences between captures mean anything) and the count of narrow lines.
    """
    n = CONFIG["fft_size"]
    sr = CONFIG["sample_rate"]
    bin_hz = sr / n
    freqs = (np.arange(n) - n // 2) * bin_hz

    half = sr * CONFIG["usable_fraction"] / 2
    ok = (np.abs(freqs) <= half) & (np.abs(freqs) >= CONFIG["dc_exclude_hz"])

    per_ch = max(1, int(round(CONFIG["channel_hz"] / bin_hz)))
    idx = np.where(ok)[0]
    # integrate in blocks of whole channels, within each contiguous side
    chans = []
    for side in (idx[idx < n // 2], idx[idx >= n // 2]):
        usable = (len(side) // per_ch) * per_ch
        if usable:
            blocks = spectrum[side[:usable]].reshape(-1, per_ch).sum(axis=1)
            chans.extend(blocks.tolist())
    floor_db = 10 * np.log10(np.median(chans))

    # Narrow persistent lines: bins well above a running median.
    k = CONFIG["line_median_bins"]
    pad = k // 2
    s = spectrum[ok]
    padded = np.pad(s, pad, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, k)
    local = np.median(windows, axis=1)
    over = s > local * 10 ** (CONFIG["line_lift_db"] / 10)
    # count groups of adjacent bins as one line
    lines = int(np.count_nonzero(over[1:] & ~over[:-1]) + (1 if over[0] else 0))
    return float(floor_db), lines


# ---------------------------------------------------------------- reporting

def recommend(rows):
    """rows: list of dicts with gain, gap, clip_pct_p, lines_p, floor_p."""
    t = CONFIG["target_gap_db"]
    safe = [r for r in rows if r["clip_pct_p"] <= CONFIG["max_clip_pct"]]
    if not safe:
        return "Every gain tested clips the ADC. Use a lower gain than 32.8 and re-test."
    for r in safe:
        if r["gap"] >= t:
            return (f"Lowest gain with a gap of {t:.0f} dB or more and no clipping: "
                    f"{r['gain']} dB (gap {r['gap']:.1f} dB).")
    best = max(safe, key=lambda r: r["gap"])
    gaps = [r["gap"] for r in safe]
    flat = len(gaps) >= 2 and (max(gaps) - gaps[0]) < 1.0
    if flat:
        return ("Gap does not grow with gain, so extra gain buys nothing. Stay at "
                f"{safe[0]['gain']} dB. The antenna is not hearing much more than the "
                "dongle's own noise here: indoors, no ground plane, or interference "
                "dominating. Re-test outdoors before drawing conclusions.")
    return (f"No gain reached {t:.0f} dB gap. Best was {best['gain']} dB with a gap of "
            f"{best['gap']:.1f} dB. Re-test outdoors, on the car roof.")


def print_table(plugged, unplugged):
    rows = []
    print(f"\n{'gain':>6}  {'plugged':>9}  {'unplug':>9}  {'gap':>6}  "
          f"{'clip %':>7}  {'lines':>5}")
    for gain in sorted(plugged):
        p = plugged[gain]
        u = unplugged.get(gain)
        if u is None:
            continue
        pf, pl = analyse_spectrum(p["spectrum"])
        uf, _ = analyse_spectrum(u["spectrum"])
        gap = pf - uf
        rows.append({"gain": p["gain_actual"], "gap": gap, "floor_p": pf,
                     "floor_u": uf, "clip_pct_p": p["clip_pct"],
                     "clip_pct_u": u["clip_pct"], "lines_p": pl})
        print(f"{p['gain_actual']:6.1f}  {pf:7.1f}dB  {uf:7.1f}dB  "
              f"{gap:5.1f}   {p['clip_pct']:6.3f}  {pl:5d}")
    if rows:
        print()
        print(check_manual_gain(rows))
        print(recommend(rows))
    return rows


def check_manual_gain(rows):
    """
    The tuner's gain mode could not be set directly (the DLL crashes on it),
    so confirm the gain really is manual: the unplugged floor, which is the
    dongle's own noise, must rise roughly dB-for-dB with the gain steps.
    """
    rows = sorted(rows, key=lambda r: r["gain"])
    if len(rows) < 2:
        return "Manual-gain check: need at least two gains."
    # R820T gain steps are not exactly their labelled size, and the dongle's
    # own noise does not all scale with tuner gain, so dB-for-dB is too strict.
    # With AGC on the floor would be flat. Require a clear rise, every step.
    total_expect = rows[-1]["gain"] - rows[0]["gain"]
    total_got = rows[-1]["floor_u"] - rows[0]["floor_u"]
    steps_up = all(b["floor_u"] > a["floor_u"] + 0.3
                   for a, b in zip(rows, rows[1:]))
    if steps_up and total_got >= 0.5 * total_expect:
        return ("Manual-gain check: PASSED. Floor rises with every gain step "
                f"({total_got:.1f} dB over a {total_expect:.1f} dB gain change), "
                "so the AGC is off.")
    return ("Manual-gain check: FAILED. The floor does not rise with gain "
            f"({total_got:.1f} dB over a {total_expect:.1f} dB gain change). "
            "The tuner may still be in AGC mode, so do not trust this table.")


def save_csv(rows):
    if not rows:
        return
    path = Path(f"gaintest_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved {path.resolve()}")
    print("Send me this file, or paste the table above.")


# ---------------------------------------------------------------- menu

def run_test():
    gains = CONFIG["gains_db"]
    print(f"\nGains: {gains} dB, {CONFIG['seconds']:.0f} s each, "
          f"centre {CONFIG['centre_hz']/1e6:.4f} MHz.")
    print("Antenna in the same spot for the whole test. SDR++ must be closed.")
    input("\nPlug the antenna IN, then press Enter... ")

    sdr = open_sdr(gains[0])
    if sdr is None:
        return
    plugged, unplugged = {}, {}
    try:
        for g in gains:
            print(f"  plugged   {g} dB ...", end=" ", flush=True)
            plugged[g] = capture_stats(sdr, g)
            print(f"done (clip {plugged[g]['clip_pct']:.3f} %)")

        input("\nNow UNPLUG the antenna from the dongle, then press Enter... ")
        for g in gains:
            print(f"  unplugged {g} dB ...", end=" ", flush=True)
            unplugged[g] = capture_stats(sdr, g)
            print("done")
    finally:
        sdr.close()

    rows = print_table(plugged, unplugged)
    save_csv(rows)


def check_environment():
    print(f"\nPython {sys.version.split()[0]}, numpy {np.__version__}")
    sdr = open_sdr(CONFIG["gains_db"][0])
    if sdr is None:
        return
    try:
        print("Dongle opened OK.")
        print(f"Sample rate {sdr.sample_rate/1e6:.3f} MS/s, "
              f"centre {sdr.center_freq/1e6:.4f} MHz")
        print(f"Gain reads back as {set_gain_direct(sdr, 32.8)} dB (asked for 32.8)")
        print(f"Valid gains: {sdr.valid_gains_db}")
        sdr.read_samples(1 << 18)
        print("Read 262144 samples OK.")
        if STUBBED:
            print(f"Missing from your DLL and stubbed: {sorted(set(STUBBED))}")
    finally:
        sdr.close()


def edit_settings():
    print(f"\nCurrent gains: {CONFIG['gains_db']}")
    raw = input("New gains, comma separated (blank to keep): ").strip()
    if raw:
        try:
            CONFIG["gains_db"] = [float(x) for x in raw.split(",")]
        except ValueError:
            print("Could not read that. Unchanged.")
    raw = input(f"Seconds per capture [{CONFIG['seconds']:.0f}]: ").strip()
    if raw:
        try:
            CONFIG["seconds"] = max(1.0, float(raw))
        except ValueError:
            print("Could not read that. Unchanged.")


def main():
    while True:
        print("\n=== RTL-SDR gain test ===")
        print("1. Check the dongle can be opened")
        print("2. Run the gain test")
        print("3. Change gains / seconds")
        print("4. Quit")
        choice = input("> ").strip()
        if choice == "1":
            check_environment()
        elif choice == "2":
            run_test()
        elif choice == "3":
            edit_settings()
        elif choice == "4":
            return


if __name__ == "__main__":
    main()
