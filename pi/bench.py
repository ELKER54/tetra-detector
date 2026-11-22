#!/usr/bin/env python3
"""
bench.py - test bench for the TETRA uplink detector

Menu-driven wrapper around detector.py and inject.py. Collects environment
info, runs false-positive and sensitivity sweeps, cross-references the two to
recommend a threshold, and writes a timestamped report.

    python bench.py

Requires detector.py and inject.py in the same folder, and numpy.
"""

import datetime as dt
import platform
import shutil
import sys
import wave
from pathlib import Path

import numpy as np

try:
    import detector
    import inject
except ImportError as e:
    sys.exit(f"Could not import detector.py / inject.py: {e}\n"
             "All three files must be in the same folder.")


# Default CFAR offsets to sweep. Wide enough to bracket the useful range.
SWEEP_OFFSETS = [12, 10, 8, 6, 5, 4, 3]

# Injected burst levels, dB above the recording's own channel noise floor.
INJECT_LEVELS = [25, 20, 16, 13, 10, 8, 6, 4, 2]

# Analyse only this much of a long recording unless told otherwise. A full
# 7 minute file takes roughly 85 seconds per pass, so a seven-point sweep on
# two files would be about 20 minutes.
DEFAULT_EXTRACT_S = 60.0

RESULTS = {}     # accumulates across menu actions, written out by the report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def wav_info(path):
    """Header metadata without reading the audio body."""
    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            return {
                "channels": wf.getnchannels(),
                "sample_width": wf.getsampwidth(),
                "sample_rate": sr,
                "frames": n,
                "duration_s": n / sr if sr else 0,
                "size_bytes": path.stat().st_size,
            }
    except Exception as e:
        return {"error": str(e)}


def list_wavs():
    return sorted(Path(".").glob("*.wav"))


def pick_wav(prompt="Select a file"):
    files = list_wavs()
    if not files:
        print("\nNo .wav files in this folder. Put your recording here, or cd to it.")
        return None

    print(f"\n{prompt}:")
    for i, f in enumerate(files, 1):
        info = wav_info(f)
        if "error" in info:
            print(f"  {i}. {f.name}  [unreadable: {info['error']}]")
        else:
            # Flag bench output so it isn't picked as a baseline by mistake,
            # which would report the planted bursts as false positives.
            tag = "  <-- bench output, contains planted bursts" \
                if f.name.startswith("bench_injected") else ""
            print(f"  {i}. {f.name}{tag}")
            print(f"      {info['duration_s']/60:.1f} min, "
                  f"{info['sample_rate']/1e6:.3f} MS/s, "
                  f"{info['size_bytes']/1e9:.2f} GB")
    print("  0. Cancel")

    try:
        choice = int(input("\n> ").strip())
    except (ValueError, EOFError):
        return None
    if choice < 1 or choice > len(files):
        return None
    return files[choice - 1]


def ask_float(prompt, default):
    raw = input(f"{prompt} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"  Not a number, using {default}")
        return default


def ask_extract_seconds(path):
    info = wav_info(path)
    full = info.get("duration_s", 0)
    print(f"\nFile is {full/60:.1f} min. Analysing all of it takes roughly "
          f"{full/60*0.2:.0f} min per pass.")
    v = ask_float("Seconds to analyse (0 for the whole file)", DEFAULT_EXTRACT_S)
    return None if v <= 0 else v


def progress_bar(frac):
    width = 30
    filled = int(width * frac)
    print(f"\r    [{'#'*filled}{'.'*(width-filled)}] {frac*100:5.1f}%",
          end="", flush=True)


def clear_progress():
    print("\r" + " " * 50 + "\r", end="")


def run_detector(path, cfar_db, centre_hz, max_seconds, quiet=False):
    cfg = dict(detector.CONFIG)
    cfg["cfar_offset_db"] = cfar_db
    cfg["centre_freq_hz"] = centre_hz
    return detector.analyse_file(
        path, cfg, max_seconds=max_seconds,
        progress=None if quiet else progress_bar,
    )


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def action_environment():
    """1. Environment and file inventory."""
    print("\n" + "=" * 68)
    print("ENVIRONMENT")
    print("=" * 68)

    print(f"  Platform      {platform.system()} {platform.release()} "
          f"({platform.machine()})")
    print(f"  Python        {platform.python_version()}")
    print(f"  numpy         {np.__version__}")
    print(f"  Processor     {platform.processor() or 'unknown'}")

    try:
        usage = shutil.disk_usage(".")
        print(f"  Disk free     {usage.free/1e9:.1f} GB of {usage.total/1e9:.1f} GB")
    except OSError:
        pass

    print(f"  Working dir   {Path('.').resolve()}")

    files = list_wavs()
    print(f"\n  WAV files found: {len(files)}")
    for f in files:
        info = wav_info(f)
        if "error" in info:
            print(f"    {f.name}: unreadable ({info['error']})")
            continue
        warn = ""
        if info["channels"] != 2:
            warn = "  <-- not baseband IQ, this is audio"
        elif info["size_bytes"] > 3.9e9:
            warn = "  <-- near the 4 GB WAV limit"
        print(f"    {f.name}")
        print(f"      {info['duration_s']/60:6.2f} min   "
              f"{info['sample_rate']/1e6:.3f} MS/s   "
              f"{info['channels']}ch/{info['sample_width']*8}bit   "
              f"{info['size_bytes']/1e9:.2f} GB{warn}")

    RESULTS["environment"] = {
        "platform": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "numpy": np.__version__,
        "files": [(f.name, wav_info(f)) for f in files],
    }
    print()


def action_quick_check():
    """2. Single run at default settings."""
    path = pick_wav("File to check")
    if not path:
        return
    centre = ask_float("Centre frequency in MHz", 382.5) * 1e6
    cfar = ask_float("CFAR offset in dB", detector.CONFIG["cfar_offset_db"])
    max_s = ask_extract_seconds(path)

    print("\nRunning...")
    r = run_detector(path, cfar, centre, max_s)
    clear_progress()
    print_run_summary(r, cfar)
    RESULTS["quick_check"] = {"file": path.name, "cfar": cfar, "result": r}


def print_run_summary(r, cfar):
    d = r["derived"]
    print(f"\n  Analysed      {r['duration_s']/60:.2f} min "
          f"({r['windows']:,} windows of {d['window_s']*1000:.3f} ms)")
    print(f"  Channels      {r['n_channels']} "
          f"({r['chan_freqs'][0]/1e6:.4f} to {r['chan_freqs'][-1]/1e6:.4f} MHz)")
    print(f"  Duration gate {d['min_windows']}-{d['max_windows']} windows "
          f"across {len(d['bands'])} burst classes")
    print(f"\n  CFAR @ {cfar:.1f} dB:")
    print(f"    raw triggers        {r['raw_triggers']:,} of "
          f"{r['cell_trials']:,} ({100*r['raw_triggers']/max(1,r['cell_trials']):.4f}%)")
    print(f"    survived gates      {r['gated_triggers']:,}")
    print(f"    wrong span          {r['rejected_span']:,}")
    print(f"    too sparse          {r['rejected_fill']:,}")
    if r.get("class_counts"):
        cls = ", ".join(f"{k:g}ms x{v}" for k, v in sorted(r["class_counts"].items()))
        print(f"    accepted classes    {cls}")
    print(f"    broadband rejected  {r['bw_rejected']:,} windows")
    print(f"    veto blocked        {r['veto_rejected']:,} batches")
    print(f"\n  DETECTIONS: {len(r['detections'])}")
    for x in r["detections"][:15]:
        print(f"    {x['time_s']:8.3f}s  {x['freq_hz']/1e6:.4f} MHz  "
              f"{x['duration_ms']:5.2f} ms  +{x['delta_db']:.1f} dB")
    if len(r["detections"]) > 15:
        print(f"    ... and {len(r['detections'])-15} more")


def action_false_positive_sweep():
    """3. Sweep CFAR on a recording you believe is empty."""
    path = pick_wav("Empty baseline recording")
    if not path:
        return
    centre = ask_float("Centre frequency in MHz", 382.5) * 1e6
    max_s = ask_extract_seconds(path)

    print("\nSweeping CFAR offsets. Every detection here is a false positive.\n")
    rows = []
    for cfar in SWEEP_OFFSETS:
        print(f"  CFAR {cfar:>4.1f} dB ", end="", flush=True)
        r = run_detector(path, cfar, centre, max_s, quiet=True)
        per_hour = len(r["detections"]) / max(r["duration_s"]/3600.0, 1e-9)
        rows.append({
            "cfar": cfar,
            "detections": len(r["detections"]),
            "per_hour": per_hour,
            "raw_pct": 100*r["raw_triggers"]/max(1, r["cell_trials"]),
            "rejected_span": r["rejected_span"],
        })
        print(f"-> {len(r['detections']):>5} FP  ({per_hour:7.1f}/hour)   "
              f"raw CFAR {rows[-1]['raw_pct']:.3f}%")

    print("\n  The duration gate does most of the rejection here. If raw CFAR"
          "\n  triggers climb but false positives stay at zero, that is the"
          "\n  gate working, not a lack of sensitivity.")

    RESULTS["false_positive_sweep"] = {"file": path.name, "rows": rows}


def action_sensitivity():
    """4. Build a positive control and find the detection floor."""
    src = pick_wav("Real recording to inject bursts into")
    if not src:
        return
    centre = ask_float("Centre frequency in MHz", 382.5) * 1e6
    offset = ask_float("Seconds into the file to extract from", 120.0)
    length = ask_float("Extract length in seconds", 30.0)
    cfar = ask_float("CFAR offset in dB", 6.0)

    out = Path("bench_injected.wav")
    print(f"\nBuilding {out} ...")

    try:
        iq, sr, sw = inject.read_extract(src, offset, length)
    except SystemExit as e:
        print(f"  {e}")
        return

    noise_power = inject.measure_channel_noise(iq, sr)
    rng = np.random.default_rng(0)
    burst_samples = int(inject.BURST_MS / 1000.0 * sr)
    spacing = length / (len(INJECT_LEVELS) + 1)

    usable_half = sr * 0.35
    candidates = []
    n = int(np.ceil((centre - usable_half - inject.RASTER_BASE_HZ)
                    / inject.RASTER_STEP_HZ))
    while True:
        f = inject.RASTER_BASE_HZ + n * inject.RASTER_STEP_HZ
        if f > centre + usable_half:
            break
        if abs(f - centre) > 100_000:
            candidates.append(f)
        n += 1

    if len(candidates) < len(INJECT_LEVELS):
        print("  Not enough usable raster channels.")
        return

    step = len(candidates) // len(INJECT_LEVELS)
    chosen = [candidates[i * step] for i in range(len(INJECT_LEVELS))]

    planted = []
    for i, (level, freq) in enumerate(zip(INJECT_LEVELS, chosen)):
        t0 = spacing * (i + 1)
        s0 = int(t0 * sr)
        if s0 + burst_samples > len(iq):
            break
        burst = inject.make_burst(burst_samples, sr, rng)
        t = np.arange(burst_samples) / sr
        shifted = burst * np.exp(2j * np.pi * (freq - centre) * t)
        iq[s0:s0+burst_samples] += (
            shifted * np.sqrt(noise_power * 10**(level/10.0))
        ).astype(np.complex64)
        planted.append({"time_s": t0, "freq_hz": freq, "level_db": level})

    peak = float(np.max(np.abs(iq)))
    if peak > 0.99:
        iq *= 0.99 / peak

    out_data = np.empty(2*len(iq), dtype=np.int16)
    out_data[0::2] = np.clip(iq.real*32767, -32768, 32767)
    out_data[1::2] = np.clip(iq.imag*32767, -32768, 32767)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(out_data.tobytes())

    print(f"  Planted {len(planted)} bursts. Running detector at "
          f"CFAR {cfar:.1f} dB ...")
    r = run_detector(out, cfar, centre, None)
    clear_progress()

    found = match_planted(planted, r["detections"])
    print_sensitivity_table(found, cfar)

    RESULTS["sensitivity"] = {
        "source": src.name, "cfar": cfar,
        "planted": planted, "found": found,
        "floor_db": detection_floor(found),
    }


def match_planted(planted, detections, time_tol_s=0.05, freq_tol_hz=30_000):
    """Pair each planted burst with a detection, if one landed close enough."""
    out = []
    for p in planted:
        hit = None
        for d in detections:
            if (abs(d["time_s"] - p["time_s"]) < time_tol_s
                    and abs(d["freq_hz"] - p["freq_hz"]) < freq_tol_hz):
                hit = d
                break
        out.append({**p, "detected": hit is not None, "detection": hit})
    return out


def detection_floor(found):
    """Weakest level that was still detected, or None if nothing was."""
    hits = [f["level_db"] for f in found if f["detected"]]
    return min(hits) if hits else None


def print_sensitivity_table(found, cfar):
    print(f"\n  {'level':>7}  {'time':>8}  {'frequency':>12}  result")
    for f in found:
        mark = "FOUND" if f["detected"] else "missed"
        print(f"  {f['level_db']:>5.0f} dB  {f['time_s']:7.2f}s  "
              f"{f['freq_hz']/1e6:9.4f} MHz  {mark}")

    floor = detection_floor(found)
    if floor is None:
        print(f"\n  Nothing detected at CFAR {cfar:.1f} dB. Either the threshold"
              "\n  is too high or something upstream is broken.")
    else:
        print(f"\n  Detection floor at CFAR {cfar:.1f} dB: "
              f"{floor:.0f} dB above the noise floor.")
        print("  A real burst weaker than that will be missed.")


def action_characterise():
    """5. Full characterisation: false positives and sensitivity together."""
    empty = pick_wav("Empty baseline recording")
    if not empty:
        return
    centre = ask_float("Centre frequency in MHz", 382.5) * 1e6
    max_s = ask_extract_seconds(empty)
    offsets = SWEEP_OFFSETS

    est = len(offsets) * 2 * ((max_s or wav_info(empty)["duration_s"]) / 300.0)
    print(f"\nThis runs {len(offsets)} offsets against two files. "
          f"Rough estimate: {est:.0f} min.")
    if input("Continue? [y/N]: ").strip().lower() != "y":
        return

    print("\nBuilding positive control from the same recording ...")
    src_len = wav_info(empty)["duration_s"]
    inj_offset = min(120.0, max(0.0, src_len / 3))
    inj_len = min(30.0, src_len - inj_offset)

    iq, sr, sw = inject.read_extract(empty, inj_offset, inj_len)
    noise_power = inject.measure_channel_noise(iq, sr)
    rng = np.random.default_rng(0)
    burst_samples = int(inject.BURST_MS / 1000.0 * sr)
    spacing = inj_len / (len(INJECT_LEVELS) + 1)

    usable_half = sr * 0.35
    candidates = []
    n = int(np.ceil((centre - usable_half - inject.RASTER_BASE_HZ)
                    / inject.RASTER_STEP_HZ))
    while True:
        f = inject.RASTER_BASE_HZ + n * inject.RASTER_STEP_HZ
        if f > centre + usable_half:
            break
        if abs(f - centre) > 100_000:
            candidates.append(f)
        n += 1
    step = max(1, len(candidates) // len(INJECT_LEVELS))
    chosen = [candidates[i*step] for i in range(len(INJECT_LEVELS))]

    planted = []
    for i, (level, freq) in enumerate(zip(INJECT_LEVELS, chosen)):
        t0 = spacing * (i + 1)
        s0 = int(t0 * sr)
        if s0 + burst_samples > len(iq):
            break
        burst = inject.make_burst(burst_samples, sr, rng)
        t = np.arange(burst_samples) / sr
        iq[s0:s0+burst_samples] += (
            burst * np.exp(2j*np.pi*(freq-centre)*t)
            * np.sqrt(noise_power * 10**(level/10.0))
        ).astype(np.complex64)
        planted.append({"time_s": t0, "freq_hz": freq, "level_db": level})

    peak = float(np.max(np.abs(iq)))
    if peak > 0.99:
        iq *= 0.99 / peak
    inj_path = Path("bench_injected.wav")
    od = np.empty(2*len(iq), dtype=np.int16)
    od[0::2] = np.clip(iq.real*32767, -32768, 32767)
    od[1::2] = np.clip(iq.imag*32767, -32768, 32767)
    with wave.open(str(inj_path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(od.tobytes())

    print(f"  {len(planted)} bursts planted.\n")
    print(f"  {'CFAR':>6}  {'false pos':>10}  {'per hour':>9}  "
          f"{'floor':>8}  {'raw CFAR':>9}")
    print("  " + "-" * 52)

    rows = []
    for cfar in offsets:
        fp = run_detector(empty, cfar, centre, max_s, quiet=True)
        sens = run_detector(inj_path, cfar, centre, None, quiet=True)
        found = match_planted(planted, sens["detections"])
        floor = detection_floor(found)

        per_hour = len(fp["detections"]) / max(fp["duration_s"]/3600.0, 1e-9)
        rows.append({
            "cfar": cfar,
            "false_positives": len(fp["detections"]),
            "per_hour": per_hour,
            "floor_db": floor,
            "raw_pct": 100*fp["raw_triggers"]/max(1, fp["cell_trials"]),
        })
        floor_s = f"{floor:.0f} dB" if floor is not None else "none"
        print(f"  {cfar:>5.1f}  {len(fp['detections']):>10}  {per_hour:>9.1f}  "
              f"{floor_s:>8}  {rows[-1]['raw_pct']:>8.3f}%")

    best = recommend(rows)
    if best:
        print(f"\n  Recommended: CFAR {best['cfar']:.1f} dB")
        print(f"    {best['false_positives']} false positives "
              f"({best['per_hour']:.1f}/hour), "
              f"detection floor {best['floor_db']:.0f} dB")
    else:
        print("\n  No offset gave both zero false positives and any detections."
              "\n  Either the recording is not actually empty, or something"
              "\n  upstream is wrong.")

    RESULTS["characterisation"] = {
        "empty_file": empty.name,
        "rows": rows,
        "recommended": best,
        "planted": planted,
    }


def recommend(rows):
    """
    Best detection floor among offsets with zero false positives.

    Sensitivity is NOT monotonic with threshold. Dropping CFAR too low floods
    the duration gate with noise triggers, which fragments genuine runs and
    makes the floor worse, not better. So pick on measured floor, and break
    ties by taking the higher CFAR for margin.
    """
    clean = [r for r in rows if r["false_positives"] == 0 and r["floor_db"] is not None]
    if not clean:
        return None
    best_floor = min(r["floor_db"] for r in clean)
    tied = [r for r in clean if r["floor_db"] == best_floor]
    return max(tied, key=lambda r: r["cfar"])


def action_report():
    """6. Write everything collected so far to a markdown file."""
    if not RESULTS:
        print("\nNothing collected yet. Run some tests first.")
        return

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(f"bench_report_{stamp}.md")
    lines = [f"# TETRA detector bench report", "",
             f"Generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]

    env = RESULTS.get("environment")
    if env:
        lines += ["## Environment", "",
                  f"- Platform: {env['platform']}",
                  f"- Python: {env['python']}",
                  f"- numpy: {env['numpy']}", "",
                  "### Recordings", ""]
        for name, info in env["files"]:
            if "error" in info:
                lines.append(f"- `{name}` unreadable: {info['error']}")
            else:
                lines.append(f"- `{name}` - {info['duration_s']/60:.2f} min, "
                             f"{info['sample_rate']/1e6:.3f} MS/s, "
                             f"{info['size_bytes']/1e9:.2f} GB")
        lines.append("")

    fps = RESULTS.get("false_positive_sweep")
    if fps:
        lines += ["## False positive sweep", "",
                  f"Source: `{fps['file']}`", "",
                  "| CFAR dB | false positives | per hour | raw CFAR % |",
                  "|---|---|---|---|"]
        for r in fps["rows"]:
            lines.append(f"| {r['cfar']:.1f} | {r['detections']} | "
                         f"{r['per_hour']:.1f} | {r['raw_pct']:.4f} |")
        lines.append("")

    sens = RESULTS.get("sensitivity")
    if sens:
        lines += ["## Sensitivity", "",
                  f"Source: `{sens['source']}`, CFAR {sens['cfar']:.1f} dB", "",
                  "| level dB | frequency MHz | detected |", "|---|---|---|"]
        for f in sens["found"]:
            lines.append(f"| {f['level_db']:.0f} | {f['freq_hz']/1e6:.4f} | "
                         f"{'yes' if f['detected'] else 'no'} |")
        floor = sens["floor_db"]
        lines += ["", f"Detection floor: "
                      f"{f'{floor:.0f} dB above noise' if floor else 'nothing detected'}",
                  ""]

    ch = RESULTS.get("characterisation")
    if ch:
        lines += ["## Full characterisation", "",
                  f"Baseline: `{ch['empty_file']}`", "",
                  "| CFAR dB | false pos | per hour | detection floor dB | raw CFAR % |",
                  "|---|---|---|---|---|"]
        for r in ch["rows"]:
            fl = f"{r['floor_db']:.0f}" if r["floor_db"] is not None else "none"
            lines.append(f"| {r['cfar']:.1f} | {r['false_positives']} | "
                         f"{r['per_hour']:.1f} | {fl} | {r['raw_pct']:.4f} |")
        rec = ch.get("recommended")
        if rec:
            lines += ["", f"**Recommended: CFAR {rec['cfar']:.1f} dB** - "
                          f"{rec['false_positives']} false positives, "
                          f"detection floor {rec['floor_db']:.0f} dB above noise.", ""]
        lines += ["", "Set `cfar_offset_db` in detector.py's CONFIG to this value.", ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWritten {path}")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

MENU = [
    ("Environment and file inventory", action_environment),
    ("Quick check (one file, one threshold)", action_quick_check),
    ("False positive sweep (empty recording)", action_false_positive_sweep),
    ("Sensitivity test (inject known bursts)", action_sensitivity),
    ("Full characterisation (both, recommends a threshold)", action_characterise),
    ("Write report", action_report),
]


def main():
    print("=" * 68)
    print("TETRA detector bench")
    print("=" * 68)
    print("\nStart with 1 to check your setup, then 5 for the useful one.")

    while True:
        print("\n" + "-" * 68)
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}. {label}")
        print("  0. Exit")

        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if raw == "0":
            return
        try:
            idx = int(raw)
        except ValueError:
            print("  Enter a number.")
            continue
        if not 1 <= idx <= len(MENU):
            print("  Out of range.")
            continue

        try:
            MENU[idx-1][1]()
        except KeyboardInterrupt:
            clear_progress()
            print("\n  Interrupted.")
        except Exception as e:
            clear_progress()
            print(f"\n  Failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
