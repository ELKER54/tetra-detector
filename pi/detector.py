#!/usr/bin/env python3
"""
detector.py - TETRA uplink burst detector (offline file analysis)

Reads an SDR++ baseband WAV recording and looks for TETRA uplink bursts
using CFAR thresholding plus three discrimination gates (channel raster,
bandwidth concentration, burst duration).

Usage:
    python detector.py recording.wav
    python detector.py recording.wav --centre 382.5e6 --verbose

Requires only numpy:
    pip install numpy

Design notes are in tetra-detector-plan.md. Key points:
  - CFAR estimates the noise floor from frequency-adjacent channels in the
    same snapshot, so it doesn't care about platform motion.
  - All three gates must pass. Power alone cannot tell TETRA from a spur.
  - Duration state persists across processing batches. A burst straddling
    a batch boundary must not be discarded.
"""

import argparse
import csv
import sys
import wave
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# CONFIG - edit these, then re-run against the same file to compare
# ---------------------------------------------------------------------------

CONFIG = {
    # Radio
    "centre_freq_hz": 382_500_000,   # what you tuned to when recording
    "fft_size": 2048,

    # TETRA uplink channel raster (ETSI: band edge 380 MHz + 12.5 kHz offset)
    "raster_base_hz": 380_012_500,
    "raster_step_hz": 25_000,
    # Integration width, NOT raster spacing. TETRA channels sit 25 kHz apart
    # but pi/4-DQPSK at 18 ksym/s occupies about 18 kHz. Integrating the full
    # 25 kHz collects roughly 1.4 dB of noise for no extra signal.
    "channel_width_hz": 18_000,

    # CFAR
    # 5.0 dB. Measured over 30 min of synthetic noise per setting: CFAR 6 and
    # 5 gave zero false positives, 4 gave 10, 3 gave 1423. The cliff sits
    # between 5 and 4, and a 120 s test missed it entirely by showing zero at
    # CFAR 4. Detection probability at 5 dB is within noise of CFAR 4, so the
    # extra dB costs nothing. Re-measure on real data before trusting it.
    "cfar_offset_db": 5.0,           # trigger this far above local noise estimate
    "guard_channels": 1,             # excluded either side of cell under test
    "training_channels": 8,          # noise estimate window either side

    # Gate 2 - bandwidth concentration
    "max_simultaneous_channels": 3,  # more than this = broadband junk, reject window

    # Gate 3 - duration. Computed from window length, see derive_params().
    #
    # TETRA uplink bursts come in two lengths. A full slot is 85/6 ms and
    # carries Normal Uplink traffic. A subslot is 85/12 ms and carries Random
    # Access and Control Uplink bursts - which is how a radio first contacts
    # the network, so registration and location updates land here. An earlier
    # version of this gate required 10+ windows and silently rejected every
    # half-slot burst, i.e. exactly the idle signalling this detector exists
    # to find.
    #
    # Multi-slot classes (28.3, 42.5, 56.7 ms) are deliberately NOT enabled by
    # default. Including them makes the accept band run from 4 ms to 74 ms,
    # which is barely a gate at all, and measurably costs false positives.
    # Detecting a vehicle only requires catching ONE burst, not all of them,
    # so the two shortest and most distinctive classes are the better trade.
    # Add the multiples only if voice traffic is being missed and the false
    # positive budget can stand it.
    # These are ON-AIR ACTIVE durations, not slot allocations. The slot is
    # 14.167 ms and the subslot 7.083 ms, but both include ramp-up, PA
    # linearisation and guard time during which there is no full-power
    # signal. Active parts, from the ETSI field counts at 18 ksym/s:
    #   control uplink / random access: 103 symbols = 5.722 ms
    #   normal uplink:                  231 symbols = 12.833 ms
    # Using the slot allocations instead made the bands 24% and 10% too long
    # and left them contiguous (5-11 and 11-22 windows), which is one band
    # with two labels. The correct values separate them (4-9 and 10-20).
    "burst_classes_ms": [5.722, 12.833],
    "duration_tolerance": 0.30,      # +/- fraction around each class

    # Fading tolerance (M-of-N). At 70 mph and 380 MHz the coherence time is
    # roughly 11 ms, comparable to a burst, so a marginal burst will contain
    # a fade. A strict consecutive-window run would be split in two and both
    # halves discarded. Instead a run survives short gaps, and is then
    # required to be mostly filled.
    "max_gap_windows": 3,            # consecutive misses tolerated mid-run
    "min_fill_ratio": 0.70,          # fired windows / span

    # Veto - blocks triggers when the whole capture lifts
    "veto_window_s": 10.0,
    "veto_lift_db": 12.0,

    # Edge handling
    "usable_fraction": 0.80,         # ignore outer 20% of capture (filter rolloff)
    "dc_notch_hz": 6_000,            # drop bins this close to centre (LO leakage)

    # Processing
    "windows_per_batch": 32,         # mirrors the live callback structure
}


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def derive_params(cfg, sample_rate):
    """Work out window timing and channel layout from the actual sample rate."""
    fft_size = cfg["fft_size"]
    bin_width = sample_rate / fft_size
    window_s = fft_size / sample_rate

    # Accept bands in windows, one per burst class. Derived rather than
    # hardcoded because window length changes with sample rate.
    tol = cfg["duration_tolerance"]
    bands = []
    for ms in cfg["burst_classes_ms"]:
        nominal = (ms / 1000.0) / window_s
        lo = max(2, int(np.floor(nominal * (1 - tol))))
        hi = int(np.ceil(nominal * (1 + tol)))
        bands.append({"label_ms": ms, "nominal": nominal, "lo": lo, "hi": hi})

    # Bands overlap once multiples are included, so the overall reject
    # thresholds are the extremes.
    return {
        "bin_width_hz": bin_width,
        "window_s": window_s,
        "bands": bands,
        "min_windows": min(b["lo"] for b in bands),
        "max_windows": max(b["hi"] for b in bands),
    }


def classify_span(span, bands):
    """Return the matching burst class label, or None if the span fits none."""
    for b in bands:
        if b["lo"] <= span <= b["hi"]:
            return b["label_ms"]
    return None


def build_channel_map(cfg, sample_rate, derived):
    """
    Work out which TETRA raster channels fall inside the usable part of the
    capture, and which FFT bins belong to each.

    Returns (channel_freqs, bin_index_lists).
    """
    centre = cfg["centre_freq_hz"]
    fft_size = cfg["fft_size"]
    bin_width = derived["bin_width_hz"]

    # Bin centre frequencies after fftshift: bin 0 is the lowest frequency.
    bin_freqs = centre + (np.arange(fft_size) - fft_size // 2) * bin_width

    half_usable = (sample_rate * cfg["usable_fraction"]) / 2.0
    low_edge = centre - half_usable
    high_edge = centre + half_usable

    # Raster channel indices covering the usable span.
    base = cfg["raster_base_hz"]
    step = cfg["raster_step_hz"]
    n_lo = int(np.ceil((low_edge - base) / step))
    n_hi = int(np.floor((high_edge - base) / step))

    channel_freqs = []
    bin_lists = []
    half_chan = cfg["channel_width_hz"] / 2.0

    # The dongle always has an LO leakage spike at the exact centre frequency.
    # Notch those bins out of every channel rather than discarding the whole
    # channel: if the capture centre sits on a raster midpoint, whole-channel
    # exclusion would silently blind the two channels either side of it.
    dc_mask = np.abs(bin_freqs - centre) < cfg["dc_notch_hz"]

    notched = 0
    for n in range(n_lo, n_hi + 1):
        f = base + n * step

        idx = np.where((np.abs(bin_freqs - f) <= half_chan) & ~dc_mask)[0]
        if len(idx) == 0:
            continue
        if np.any(np.abs(bin_freqs[np.abs(bin_freqs - f) <= half_chan] - centre)
                  < cfg["dc_notch_hz"]):
            notched += 1

        channel_freqs.append(f)
        bin_lists.append(idx)

    return np.array(channel_freqs), bin_lists, notched


# ---------------------------------------------------------------------------
# WAV reading
# ---------------------------------------------------------------------------

def open_iq_wav(path):
    """Open an SDR++ baseband WAV and return (wave object, sample_rate, sampwidth)."""
    wf = wave.open(str(path), "rb")
    if wf.getnchannels() != 2:
        sys.exit(f"Expected 2 channels (I and Q), got {wf.getnchannels()}. "
                 "Is this a baseband recording rather than audio?")
    if wf.getsampwidth() not in (1, 2):
        sys.exit(f"Unsupported sample width {wf.getsampwidth()} bytes.")
    return wf, wf.getframerate(), wf.getsampwidth()


def read_iq_chunk(wf, n_frames, sampwidth):
    """Read n_frames of interleaved I/Q and return a complex64 array."""
    raw = wf.readframes(n_frames)
    if not raw:
        return np.array([], dtype=np.complex64)

    if sampwidth == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        # 8-bit WAV is unsigned, centred on 128.
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0

    if len(data) % 2:
        data = data[:-1]
    iq = data[0::2] + 1j * data[1::2]
    return iq.astype(np.complex64)


# ---------------------------------------------------------------------------
# DSP
# ---------------------------------------------------------------------------

def channel_powers_db(iq_batch, fft_size, bin_lists, window):
    """
    Vectorised batched FFT, then integrate power across the bins belonging to
    each raster channel.

    Returns array of shape (n_windows, n_channels) in dB.
    """
    n_windows = len(iq_batch) // fft_size
    batch = iq_batch[:n_windows * fft_size].reshape(n_windows, fft_size)

    # Hann window before FFT. Without it, a strong signal smears across bins
    # and defeats the bandwidth-concentration gate.
    spectra = np.fft.fft(batch * window, axis=1)
    spectra = np.fft.fftshift(spectra, axes=1)
    power = np.abs(spectra) ** 2

    n_channels = len(bin_lists)
    out = np.empty((n_windows, n_channels), dtype=np.float32)
    for c, idx in enumerate(bin_lists):
        out[:, c] = power[:, idx].sum(axis=1)

    return 10.0 * np.log10(out + 1e-20)


def cfar_trigger(chan_db, cfg):
    """
    Cell-averaging CFAR across channels, using the median for robustness.

    For each channel, estimate noise from training channels either side,
    skipping guard channels adjacent to the cell under test. Training windows
    are truncated at the band edges rather than wrapping, because wrapping
    would silently compare against the far end of the spectrum.

    Returns (trigger_mask, noise_estimate_db), both shape (n_windows, n_channels).
    """
    n_windows, n_channels = chan_db.shape
    guard = cfg["guard_channels"]
    train = cfg["training_channels"]

    noise_est = np.empty_like(chan_db)

    for c in range(n_channels):
        lo_start = max(0, c - guard - train)
        lo_end = max(0, c - guard)
        hi_start = min(n_channels, c + guard + 1)
        hi_end = min(n_channels, c + guard + train + 1)

        parts = []
        if lo_end > lo_start:
            parts.append(chan_db[:, lo_start:lo_end])
        if hi_end > hi_start:
            parts.append(chan_db[:, hi_start:hi_end])

        if not parts:
            noise_est[:, c] = chan_db[:, c]  # degenerate, shouldn't happen
            continue

        training = np.concatenate(parts, axis=1)
        noise_est[:, c] = np.median(training, axis=1)

    return chan_db > (noise_est + cfg["cfar_offset_db"]), noise_est


def apply_bandwidth_gate(triggers, cfg):
    """
    Gate 2. A genuine TETRA burst occupies one 25 kHz channel. A broadband
    impulse lights up many at once, so reject any window where too many
    channels fire together.
    """
    per_window = triggers.sum(axis=1)
    bad = per_window > cfg["max_simultaneous_channels"]
    triggers = triggers.copy()
    triggers[bad, :] = False
    return triggers, int(bad.sum())


# ---------------------------------------------------------------------------
# Duration state machine
# ---------------------------------------------------------------------------

class DurationTracker:
    """
    Gate 3. Tracks a run of triggered windows per channel and emits a
    detection when the run ends with a span matching a TETRA burst class.

    Two things this must get right:

    1. State persists across batches. A burst can straddle a batch boundary;
       resetting per batch produces detections clustered at exactly the batch
       interval.
    2. A run survives short gaps. Multipath fading at vehicle speed puts a
       null inside a burst, and a strict consecutive-window rule would split
       one burst into two rejected fragments.
    """

    def __init__(self, n_channels, bands, max_gap, min_fill):
        self.bands = bands
        self.max_gap = max_gap
        self.min_fill = min_fill

        self.active = np.zeros(n_channels, dtype=bool)
        self.run_start = np.zeros(n_channels, dtype=np.int64)
        self.last_hit = np.zeros(n_channels, dtype=np.int64)
        self.hits = np.zeros(n_channels, dtype=np.int32)
        self.gap = np.zeros(n_channels, dtype=np.int32)
        self.peak_db = np.full(n_channels, -np.inf, dtype=np.float32)
        self.peak_delta = np.full(n_channels, -np.inf, dtype=np.float32)

        self.rejected_span = 0      # span matched no burst class
        self.rejected_fill = 0      # too sparse within its span
        self.class_counts = {}      # label_ms -> accepted count

    def reset_channel(self, c):
        self.active[c] = False
        self.hits[c] = 0
        self.gap[c] = 0
        self.peak_db[c] = -np.inf
        self.peak_delta[c] = -np.inf

    def close_run(self, c):
        """Judge a finished run. Returns a detection dict or None."""
        span = int(self.last_hit[c] - self.run_start[c]) + 1
        hits = int(self.hits[c])
        label = classify_span(span, self.bands)

        if label is None:
            self.rejected_span += 1
            result = None
        elif hits / span < self.min_fill:
            self.rejected_fill += 1
            result = None
        else:
            self.class_counts[label] = self.class_counts.get(label, 0) + 1
            result = {
                "start_window": int(self.run_start[c]),
                "channel_index": int(c),
                "length_windows": span,
                "hits": hits,
                "burst_class_ms": label,
                "peak_db": float(self.peak_db[c]),
                "peak_delta_db": float(self.peak_delta[c]),
            }

        self.reset_channel(c)
        return result

    def update(self, triggers, chan_db, noise_db, window_offset):
        """Process one batch. Returns completed detections."""
        detections = []

        for w in range(triggers.shape[0]):
            fired = triggers[w]
            global_w = window_offset + w

            # Active channels that missed this window: extend the gap, and
            # close the run if the gap has run past tolerance.
            missed = self.active & ~fired
            if missed.any():
                self.gap[missed] += 1
                for c in np.flatnonzero(missed & (self.gap > self.max_gap)):
                    d = self.close_run(c)
                    if d:
                        detections.append(d)

            if not fired.any():
                continue

            # New runs.
            starting = fired & ~self.active
            if starting.any():
                self.run_start[starting] = global_w
                self.active[starting] = True

            # Any fire clears the gap and advances the run.
            self.gap[fired] = 0
            self.hits[fired] += 1
            self.last_hit[fired] = global_w

            delta = chan_db[w] - noise_db[w]
            better = fired & (chan_db[w] > self.peak_db)
            self.peak_db[better] = chan_db[w][better]
            self.peak_delta[better] = delta[better]

        return detections

    def flush(self):
        """Close any runs still open at end of file."""
        out = []
        for c in np.flatnonzero(self.active):
            d = self.close_run(c)
            if d:
                out.append(d)
        return out


def merge_adjacent(detections, window_s):
    """
    A real burst has spectral skirts that spill into neighbouring channels, so
    one transmission can trigger two or three adjacent raster channels at once.
    Collapse those into a single detection on the strongest channel.
    """
    if not detections:
        return []

    ordered = sorted(detections, key=lambda d: (d["start_window"], d["channel_index"]))
    merged = []

    for d in ordered:
        placed = False
        for m in merged:
            overlaps = (abs(d["start_window"] - m["start_window"])
                        <= max(d["length_windows"], m["length_windows"]))
            adjacent = abs(d["channel_index"] - m["channel_index"]) <= 1
            if overlaps and adjacent:
                if d["peak_db"] > m["peak_db"]:
                    m.update(d)
                placed = True
                break
        if not placed:
            merged.append(dict(d))

    return merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyse_file(path, cfg, max_seconds=None, progress=None):
    """
    Run the full detection chain over a WAV file and return a results dict.

    Shared by detector.py's CLI and bench.py, so both measure identically.
    Pass max_seconds to analyse only the first N seconds of a long recording.
    """
    wf, sample_rate, sampwidth = open_iq_wav(path)
    total_frames = wf.getnframes()
    if max_seconds is not None:
        total_frames = min(total_frames, int(max_seconds * sample_rate))

    derived = derive_params(cfg, sample_rate)
    chan_freqs, bin_lists, n_notched = build_channel_map(cfg, sample_rate, derived)
    if len(chan_freqs) == 0:
        wf.close()
        raise ValueError("No TETRA raster channels inside this capture. "
                         "Check the centre frequency.")

    fft_size = cfg["fft_size"]
    hann = np.hanning(fft_size).astype(np.float32)
    tracker = DurationTracker(len(chan_freqs), derived["bands"],
                              cfg["max_gap_windows"], cfg["min_fill_ratio"])

    batch_samples = fft_size * cfg["windows_per_batch"]
    veto_batches = max(1, int(cfg["veto_window_s"] /
                              (derived["window_s"] * cfg["windows_per_batch"])))
    veto_history = []

    detections = []
    windows_done = batches_done = 0
    bw_rejected = veto_rejected = raw_triggers = gated_triggers = 0
    frames_read = 0

    while frames_read < total_frames:
        want = min(batch_samples, total_frames - frames_read)
        iq = read_iq_chunk(wf, want, sampwidth)
        if len(iq) < fft_size:
            break
        frames_read += len(iq)

        chan_db = channel_powers_db(iq, fft_size, bin_lists, hann)
        triggers, noise_db = cfar_trigger(chan_db, cfg)
        raw_triggers += int(triggers.sum())

        triggers, n_bw = apply_bandwidth_gate(triggers, cfg)
        bw_rejected += n_bw

        total_db = float(np.median(chan_db))
        if len(veto_history) >= veto_batches:
            baseline = float(np.median(veto_history[-veto_batches:]))
            if total_db > baseline + cfg["veto_lift_db"]:
                triggers[:, :] = False
                veto_rejected += 1
        veto_history.append(total_db)

        gated_triggers += int(triggers.sum())
        detections.extend(tracker.update(triggers, chan_db, noise_db, windows_done))

        windows_done += chan_db.shape[0]
        batches_done += 1
        if progress and batches_done % 500 == 0:
            progress(frames_read / max(1, total_frames))

    detections.extend(tracker.flush())
    wf.close()

    raw_count = len(detections)
    detections = merge_adjacent(detections, derived["window_s"])

    results = []
    for d in detections:
        results.append({
            "time_s": d["start_window"] * derived["window_s"],
            "freq_hz": float(chan_freqs[d["channel_index"]]),
            "duration_ms": d["length_windows"] * derived["window_s"] * 1000.0,
            "burst_class_ms": d["burst_class_ms"],
            "peak_db": d["peak_db"],
            "delta_db": d["peak_delta_db"],
        })

    return {
        "path": Path(path),
        "sample_rate": sample_rate,
        "duration_s": frames_read / sample_rate,
        "derived": derived,
        "n_channels": len(chan_freqs),
        "chan_freqs": chan_freqs,
        "n_notched": n_notched,
        "windows": windows_done,
        "batches": batches_done,
        "raw_triggers": raw_triggers,
        "gated_triggers": gated_triggers,
        "cell_trials": windows_done * len(chan_freqs),
        "rejected_span": tracker.rejected_span,
        "rejected_fill": tracker.rejected_fill,
        "class_counts": dict(tracker.class_counts),
        "bw_rejected": bw_rejected,
        "veto_rejected": veto_rejected,
        "merged": raw_count - len(detections),
        "detections": results,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", type=Path, help="SDR++ baseband WAV recording")
    ap.add_argument("--centre", type=float, default=None,
                    help="centre frequency in Hz, overrides CONFIG")
    ap.add_argument("--cfar", type=float, default=None,
                    help="CFAR offset in dB, overrides CONFIG")
    ap.add_argument("--csv", type=Path, default=Path("detections.csv"))
    ap.add_argument("--verbose", action="store_true",
                    help="report near-misses and per-gate rejection counts")
    args = ap.parse_args()

    cfg = dict(CONFIG)
    if args.centre is not None:
        cfg["centre_freq_hz"] = args.centre
    if args.cfar is not None:
        cfg["cfar_offset_db"] = args.cfar

    if not args.wav.exists():
        sys.exit(f"File not found: {args.wav}")

    wf, sample_rate, sampwidth = open_iq_wav(args.wav)
    total_frames = wf.getnframes()
    duration_s = total_frames / sample_rate

    derived = derive_params(cfg, sample_rate)
    chan_freqs, bin_lists, n_notched = build_channel_map(cfg, sample_rate, derived)

    if len(chan_freqs) == 0:
        sys.exit("No TETRA raster channels fall inside this capture. "
                 "Check --centre matches what you actually recorded at.")

    fft_size = cfg["fft_size"]
    hann = np.hanning(fft_size).astype(np.float32)

    print(f"File:          {args.wav.name}")
    print(f"Sample rate:   {sample_rate/1e6:.3f} MS/s")
    print(f"Duration:      {duration_s/60:.1f} min ({total_frames:,} samples)")
    print(f"Centre:        {cfg['centre_freq_hz']/1e6:.4f} MHz")
    print(f"Window:        {derived['window_s']*1000:.3f} ms "
          f"({derived['bin_width_hz']:.1f} Hz bins)")
    bands_s = ", ".join(f"{b['label_ms']:g}ms={b['lo']}-{b['hi']}w"
                        for b in derived["bands"])
    print(f"Duration gate: {bands_s}")
    print(f"               gap<={cfg['max_gap_windows']}w, "
          f"fill>={cfg['min_fill_ratio']:.0%}")
    print(f"Channels:      {len(chan_freqs)} "
          f"({chan_freqs[0]/1e6:.4f} to {chan_freqs[-1]/1e6:.4f} MHz)")
    print(f"CFAR offset:   {cfg['cfar_offset_db']:.1f} dB")
    if n_notched:
        print(f"DC notch:      {n_notched} channel(s) partially notched at centre")
    print()

    tracker = DurationTracker(len(chan_freqs), derived["bands"],
                              cfg["max_gap_windows"], cfg["min_fill_ratio"])

    batch_samples = fft_size * cfg["windows_per_batch"]
    veto_batches = max(1, int(cfg["veto_window_s"] /
                              (derived["window_s"] * cfg["windows_per_batch"])))
    veto_history = []

    detections = []
    windows_done = 0
    batches_done = 0
    bw_rejected = 0
    veto_rejected = 0
    raw_triggers = 0          # CFAR hits before any gate
    gated_triggers = 0        # surviving bandwidth gate and veto

    while True:
        iq = read_iq_chunk(wf, batch_samples, sampwidth)
        if len(iq) < fft_size:
            break

        chan_db = channel_powers_db(iq, fft_size, bin_lists, hann)
        triggers, noise_db = cfar_trigger(chan_db, cfg)
        raw_triggers += int(triggers.sum())

        triggers, n_bw = apply_bandwidth_gate(triggers, cfg)
        bw_rejected += n_bw

        # Veto: if the whole capture has lifted, CFAR's relative comparison
        # is meaningless. Blocks triggers, never creates them.
        total_db = float(np.median(chan_db))
        if len(veto_history) >= veto_batches:
            baseline = float(np.median(veto_history[-veto_batches:]))
            if total_db > baseline + cfg["veto_lift_db"]:
                triggers[:, :] = False
                veto_rejected += 1
        veto_history.append(total_db)

        gated_triggers += int(triggers.sum())
        detections.extend(
            tracker.update(triggers, chan_db, noise_db, windows_done)
        )

        windows_done += chan_db.shape[0]
        batches_done += 1

        if batches_done % 500 == 0:
            pct = 100.0 * (windows_done * fft_size) / total_frames
            print(f"  {pct:5.1f}%  {len(detections)} detections so far", end="\r")

    detections.extend(tracker.flush())
    wf.close()
    print(" " * 60, end="\r")

    raw_count = len(detections)
    detections = merge_adjacent(detections, derived["window_s"])

    # Write results
    with open(args.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "frequency_mhz", "duration_ms", "class_ms",
                    "peak_db", "delta_above_noise_db"])
        for d in detections:
            t = d["start_window"] * derived["window_s"]
            w.writerow([
                f"{t:.3f}",
                f"{chan_freqs[d['channel_index']]/1e6:.4f}",
                f"{d['length_windows'] * derived['window_s'] * 1000:.2f}",
                f"{d['burst_class_ms']:g}",
                f"{d['peak_db']:.1f}",
                f"{d['peak_delta_db']:.1f}",
            ])

    cell_trials = windows_done * len(chan_freqs)
    print(f"Processed {windows_done:,} windows ({batches_done:,} batches)")
    print(f"CFAR raw triggers: {raw_triggers:,} of {cell_trials:,} cell-trials "
          f"({100.0*raw_triggers/max(1,cell_trials):.4f}%)")
    print(f"Surviving bandwidth gate and veto: {gated_triggers:,}")
    print(f"Runs rejected by duration gate: "
          f"{tracker.rejected_span:,} wrong span, "
          f"{tracker.rejected_fill:,} too sparse")
    if tracker.class_counts:
        cls = ", ".join(f"{k:g}ms x{v}"
                        for k, v in sorted(tracker.class_counts.items()))
        print(f"Accepted burst classes: {cls}")
    print(f"Bandwidth gate rejected {bw_rejected:,} windows as broadband")
    print(f"Veto blocked {veto_rejected:,} batches")
    print(f"Merged {raw_count - len(detections):,} adjacent-channel duplicates")
    print()
    print(f"DETECTIONS: {len(detections)}")

    if detections:
        print(f"Written to {args.csv}")
        print()
        for d in detections[:20]:
            t = d["start_window"] * derived["window_s"]
            print(f"  {t:8.3f}s  {chan_freqs[d['channel_index']]/1e6:.4f} MHz  "
                  f"{d['length_windows'] * derived['window_s'] * 1000:5.2f} ms  "
                  f"[{d['burst_class_ms']:g}ms]  +{d['peak_delta_db']:.1f} dB")
        if len(detections) > 20:
            print(f"  ... and {len(detections) - 20} more")
        print()
        print("On a recording you believe is empty, every one of these is a "
              "false positive. Raise cfar_offset_db and re-run until it reads "
              "zero, then note the value you needed.")
    else:
        print()
        print("Clean. On an empty baseline that is the correct result, but it "
              "does not prove sensitivity. Lower cfar_offset_db until "
              "detections appear, to find where your noise starts breaking "
              "through, then set the real threshold a few dB above that.")


if __name__ == "__main__":
    main()
