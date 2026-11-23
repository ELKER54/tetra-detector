#!/usr/bin/env python3
"""
live.py - live TETRA uplink burst detector with beeps

Reads the dongle directly, hops across three window positions to cover the
whole 380-385 MHz uplink band, and runs the same detection chain as
detector.py (CFAR, bandwidth gate, duration gate). Each detection is then
judged (see Alerter): TETRA frame timing, wideband interference, quiet zones.
It beeps when a channel becomes active or gets much stronger, with a higher
pitch for a stronger (closer) signal. Gain backs off on overload and recovers.

Every detection goes to detections.csv with its status. Alerts also save the
raw IQ around the burst as a WAV that detector.py can re-analyse.

Needs detector.py and gaintest.py in the same folder. Close other SDR programs.

Honest limits:
  - The rules were tuned on a few hours of real driving data in one area.
    Other areas and other interference may need retuning.
  - Hopping means each position is watched about a third of the time, so
    roughly one burst in three is seen.
  - Signal strength says how close a radio is, not whether it is moving:
    radios parked at a station alert as strongly as a passing vehicle.
  - This only detects energy that looks like a TETRA burst. It does not and
    cannot decode anything.
"""

import csv
import multiprocessing as mp
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

import detector
import gaintest
import tetra_link

try:                                   # keeps complex64, no double-precision copy
    from scipy import fft as _scipy_fft

    def _fft_rows(x):
        return _scipy_fft.fft(x, axis=1, workers=1)
    FFT_LIB = "scipy (fast)"
except Exception:
    FFT_LIB = "numpy (slower: pip install scipy)"
    def _fft_rows(x):
        return np.fft.fft(x, axis=1)

CONFIG = {
    "sample_rate": 2_400_000,
    "gain_db": 32.8,              # starting gain; backs off by itself on overload.
                                  # Higher gains buy under 1 dB on a metal ground plane.
    "dwell_s": 0.8,               # time on each window position per hop
    "chunk_samples": 1 << 18,     # about 109 ms; bigger chunks cost less per sample
    "settle_samples": 1 << 17,    # about 55 ms discarded after each retune
    "cfar_offset_db": 5.0,
    "max_clip_pct": 0.1,
    "backoff_db": 3.0,
    "beep": True,
    "beep_cooldown_s": 1.0,
    "save_snippets": True,
    "max_snippets": 150,          # about 2 MB each
    "min_free_mb": 500,           # below this, stop saving snippets
    "prune_free_mb": 2000,        # headless start: delete oldest runs until this is free
    "status_every_s": 30.0,
    "queue_size": 6,              # whole dwells (about 4 MB each)
    # What counts as a radio, and when to beep. Tuned on the first car runs.
    # TETRA radios transmit on a 56.67 ms frame. On real channels 84 % of gaps
    # between bursts in one dwell fitted that grid; on interference 5-12 %.
    # Real drives also showed many gaps a half-slot (7.08 ms) off whole frames,
    # e.g. 49.6 ms: short bursts in the second half of a slot. Counting those
    # as fitting stopped strong real radios (up to +42 dB) being muted as
    # "unlocked", with a tighter tolerance so interference still fails.
    "frame_tol_ms": 2.0,          # gap counts as fitting if within this of the grid
    "frame_offsets_ms": (0.0, 7.083),   # whole frames, or whole frames +/- half a slot
    "lock_window_s": 120.0,       # how far back the fit is judged
    "unlock_min_gaps": 4,         # gaps needed before a channel can be ruled out
    "unlock_fit": 0.3,            # below this fraction fitting: not a radio
    "wideband_channels": 3,       # this many channels in one dwell: interference
    "quiet_gap_s": 30.0,          # channel silent this long, then active: beep
    "rise_db": 10.0,              # within an episode, beep again if this much stronger
    # (and always when it moves up a band: weak -> medium -> STRONG)
    "tone_db": (15.0, 30.0),      # low / mid / high tone boundaries
    # Gain recovery after an overload back-off.
    "recover_s": 30.0,            # clean time before stepping gain back up
    "recover_max_s": 300.0,       # wait doubles each time a raise overloads again
    # Quiet zones (Hz, low to high). Detections here only beep if the channel
    # keeps TETRA frame timing ("locked"); otherwise they're logged as quiet.
    # Home tests showed a repeating false source across 383.85-384.20 MHz, but
    # it fails the timing test 98 % of the time, while a real radio seen in
    # the zone on a test drive (no beep at the time) passed it.
    "quiet_zones": [(383_850_000, 384_200_000)],
    # If processing is more than this many seconds behind at the start of a
    # dwell, that dwell is skipped to catch up, so beeps are never stale.
    "max_lag_s": 1.0,
    # No dwell from the dongle this long: it has hung (e.g. a USB power dip).
    # The run exits so systemd restarts it; a hung read never times out by itself.
    "stall_s": 20.0,
    # Dwells skipped this recently: tell the phone the Pi is falling behind.
    "behind_flag_s": 30.0,
    "workers": 0,                 # processes for the maths; 0 = one per CPU core
    "snippet_pad_s": 0.2,         # raw data kept either side of a burst
}


# ---------------------------------------------------------------- windows

def window_centres():
    """
    Three centres on raster midpoints, so the dongle's centre spike lands
    between two channels and not on one. Each covers about 68 channels plus
    margin: 200 channels, 380.0125 to 384.9875 MHz.
    """
    base = detector.CONFIG["raster_base_hz"]
    step = detector.CONFIG["raster_step_hz"]
    return [int(base + k * step) for k in (33.5, 99.5, 165.5)]


def build_windows():
    sr = CONFIG["sample_rate"]
    centres = window_centres()
    wins = []
    for i, c in enumerate(centres):
        cfg = dict(detector.CONFIG)
        cfg["centre_freq_hz"] = c
        cfg["cfar_offset_db"] = CONFIG["cfar_offset_db"]
        derived = detector.derive_params(cfg, sr)
        # derive_params rounds the lower bound DOWN, which let 4-window (3.4 ms)
        # runs through the 5.7 ms class. Round it up. This lifts the short
        # class floor from 4 to 5 windows and the long one from 10 to 11.
        # Effect on detection probability has not been re-measured.
        tol = cfg["duration_tolerance"]
        for b in derived["bands"]:
            b["lo"] = max(b["lo"], int(np.ceil(b["nominal"] * (1 - tol))))
        freqs, bins, _ = detector.build_channel_map(cfg, sr, derived)
        # Each channel belongs to the window whose centre is nearest, so an
        # overlap is never reported twice.
        owner = np.array([int(np.argmin([abs(f - cc) for cc in centres]))
                          for f in freqs])
        n_fft = cfg["fft_size"]
        # 0/1 matrix summing FFT bins into channels (one matmul instead of a
        # Python loop). Bin lists are in fftshift order; map back to FFT order.
        mat = np.zeros((n_fft, len(bins)), dtype=np.float32)
        for ch, idx in enumerate(bins):
            mat[(np.asarray(idx) - n_fft // 2) % n_fft, ch] = 1.0
        wins.append({"i": i, "centre": c, "cfg": cfg, "derived": derived,
                     "chan_mat": mat,
                     "train_idx": train_index(len(bins), cfg),
                     "freqs": freqs, "bins": bins,
                     # Only the TETRA uplink band: 380-385 MHz.
                     "owned": (owner == i) & (freqs >= 380_000_000)
                              & (freqs < 385_000_000),
                     "veto": []})
    return wins


# ---------------------------------------------------------------- fast maths
# Vectorised equivalents of detector.channel_powers_db and detector.cfar_trigger
# (checked against them on random data; see the notes in the delivery message).

def train_index(n_ch, cfg):
    """(n_ch, 2*train) training-cell indices; n_ch marks 'no cell' (edges)."""
    g, t = cfg["guard_channels"], cfg["training_channels"]
    idx = np.full((n_ch, 2 * t), n_ch, dtype=np.int64)
    for c in range(n_ch):
        cells = list(range(max(0, c - g - t), max(0, c - g))) + \
                list(range(min(n_ch, c + g + 1), min(n_ch, c + g + t + 1)))
        idx[c, :len(cells)] = cells
    return idx


def fast_channel_db(iq, win, hann):
    n = win["cfg"]["fft_size"]
    nw = len(iq) // n
    sp = _fft_rows(iq[:nw * n].reshape(nw, n) * hann)
    power = sp.real * sp.real + sp.imag * sp.imag
    return (10.0 * np.log10(power @ win["chan_mat"] + 1e-20)).astype(np.float32)


def fast_cfar(chan_db, win, cfg):
    n_w, n_ch = chan_db.shape
    idx = win["train_idx"]
    padded = np.concatenate(
        [chan_db, np.full((n_w, 1), np.nan, dtype=chan_db.dtype)], axis=1)
    cells = np.sort(padded[:, idx], axis=2)            # NaN sorts to the end
    k = (idx < n_ch).sum(axis=1)                        # valid cells per channel
    hi = np.maximum(k - 1, 0)
    lo_i = (hi // 2)[None, :, None]
    hi_i = (k // 2).clip(max=cells.shape[2] - 1)[None, :, None]
    lo_i = np.broadcast_to(lo_i, (n_w, n_ch, 1))
    hi_i = np.broadcast_to(hi_i[:, :, :], (n_w, n_ch, 1))
    med = 0.5 * (np.take_along_axis(cells, lo_i, 2)[..., 0]
                 + np.take_along_axis(cells, hi_i, 2)[..., 0])
    noise = np.where(k[None, :] > 0, med, chan_db).astype(chan_db.dtype)
    return chan_db > (noise + cfg["cfar_offset_db"]), noise


# ---------------------------------------------------------------- beep

class Beeper:
    """Three alerts: low single pip (weak), mid double, high triple (strong)."""

    TONES = ((600, 1), (1000, 2), (1600, 3))      # (Hz, pips)

    def __init__(self):
        self.last = 0.0
        self.last_tone = -1
        self.cmd = None
        self.paths = []
        for k, (hz, pips) in enumerate(self.TONES):
            p = Path(tempfile.gettempdir()) / f"tetra_live_beep{k}.wav"
            self._make_tone(p, hz, pips)
            self.paths.append(p)
        for tool, args in (("paplay", []), ("pw-play", []), ("aplay", ["-q"])):
            if shutil.which(tool):
                self.cmd = [tool] + args
                break

    @staticmethod
    def _make_tone(path, hz, pips, pip_s=0.12, gap_s=0.06, rate=44100):
        t = np.arange(int(pip_s * rate)) / rate
        env = np.minimum(1.0, np.minimum(t, pip_s - t) / 0.01)
        pip = 0.6 * env * np.sin(2 * np.pi * hz * t)
        gap = np.zeros(int(gap_s * rate))
        sig = np.concatenate([np.concatenate([pip, gap]) for _ in range(pips)])
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((sig * 32767).astype(np.int16).tobytes())

    def beep(self, tone=1, force=False):
        now = time.time()
        # A stronger alert may interrupt the cooldown; a weaker one may not.
        if (not force and now - self.last < CONFIG["beep_cooldown_s"]
                and tone <= self.last_tone):
            return
        self.last, self.last_tone = now, tone
        if self.cmd:
            subprocess.Popen(self.cmd + [str(self.paths[tone])],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print("\a", end="", flush=True)


# ---------------------------------------------------------------- Pi health

THROTTLE_BITS = {0x1: "under-voltage NOW", 0x2: "CPU capped NOW", 0x4: "throttled NOW",
                 0x8: "temperature limit NOW", 0x10000: "under-voltage happened",
                 0x20000: "CPU cap happened", 0x40000: "throttling happened",
                 0x80000: "temperature limit happened"}


def pi_health():
    """(temp C, throttled flags) on a Raspberry Pi, else (None, None)."""
    if not shutil.which("vcgencmd"):
        return None, None
    try:
        t = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True,
                           text=True, timeout=2).stdout       # temp=48.3'C
        f = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                           text=True, timeout=2).stdout       # throttled=0x0
        return float(t.split("=")[1].split("'")[0]), int(f.split("=")[1], 16)
    except Exception:
        return None, None


def describe_throttle(flags):
    if not flags:
        return "none"
    return ", ".join(v for k, v in THROTTLE_BITS.items() if flags & k)


# ---------------------------------------------------------------- alerting

FRAME_MS = 4 * 85 / 6.0     # TETRA TDMA frame: 4 slots of 255 symbols at 18 ksym/s


class Alerter:
    """
    Turns one dwell's detections into statuses:
      beep      new activity on a channel, or it got much stronger
      active    same channel still going, no new beep
      quiet     in a quiet zone (known local interference) without TETRA timing
      wideband  many channels at once: interference, not a radio
      unlocked  bursts on this channel don't keep TETRA frame timing
    Pure Python with no dongle, so replay.py can run it over saved logs.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg or CONFIG
        self.gaps = {}          # channel Hz -> [(time, fits_frame)]
        self.episode = {}       # channel Hz -> [last time, max dB, highest tone announced]

    def _quiet(self, f):
        return any(lo <= f < hi for lo, hi in self.cfg["quiet_zones"])

    def fits_grid(self, gap_ms):
        """Does a gap between bursts fit the TETRA timing? Whole frames, or
        whole frames plus or minus any of frame_offsets_ms (half-slots etc.)."""
        r = gap_ms % FRAME_MS
        tol = self.cfg["frame_tol_ms"]
        return any(abs(r - o) <= tol or abs(r - (FRAME_MS - o)) <= tol
                   for o in self.cfg["frame_offsets_ms"])

    def frame_state(self, f, now):
        cfg = self.cfg
        h = [g for g in self.gaps.get(f, []) if g[0] >= now - cfg["lock_window_s"]]
        self.gaps[f] = h
        n = len(h)
        fit = sum(g[1] for g in h) / n if n else 0.0
        if n >= cfg["unlock_min_gaps"] and fit < cfg["unlock_fit"]:
            return "unlocked"
        if n >= 2 and fit >= 0.5:
            return "locked"
        return "unknown"

    def tone_for(self, db):
        lo, hi = self.cfg["tone_db"]
        return 0 if db < lo else (1 if db < hi else 2)

    def process(self, dets):
        """dets: dicts with t (epoch s), f (Hz), db, all from one dwell.
        Returns [(status, tone or None, frame_state)] in the same order."""
        cfg = self.cfg
        by_ch = {}
        for i, d in enumerate(dets):
            by_ch.setdefault(d["f"], []).append(i)
        for f, idx in by_ch.items():
            ts = sorted(dets[i]["t"] for i in idx)
            for a, b in zip(ts, ts[1:]):
                dt = (b - a) * 1000.0
                if dt < 20.0:
                    continue                       # two fragments of one burst
                self.gaps.setdefault(f, []).append((b, self.fits_grid(dt)))
        t0 = min(d["t"] for d in dets) if dets else 0.0
        n_ch = sum(1 for f in by_ch
                   if not self._quiet(f) or self.frame_state(f, t0) == "locked")
        wide = n_ch >= cfg["wideband_channels"]

        out = [None] * len(dets)
        for i in sorted(range(len(dets)), key=lambda k: dets[k]["t"]):
            f, t, db = dets[i]["f"], dets[i]["t"], dets[i]["db"]
            state = self.frame_state(f, t)
            if self._quiet(f) and state != "locked":
                out[i] = ("quiet", None, state)
                continue
            if wide and state != "locked":
                out[i] = ("wideband", None, state)
                continue
            if state == "unlocked":
                out[i] = ("unlocked", None, state)
                continue
            ep = self.episode.get(f)
            tone = self.tone_for(db)
            if ep is None or t - ep[0] > cfg["quiet_gap_s"] or t < ep[0] - 5.0:
                self.episode[f] = [t, db, tone]
                out[i] = ("beep", tone, state)
            elif tone > ep[2] or db >= ep[1] + cfg["rise_db"]:
                # Getting closer. A gradual climb never clears rise_db, so the band
                # change matters: on a test drive an ambulance went +8 -> +21 dB unheard.
                ep[0], ep[1], ep[2] = t, max(ep[1], db), max(ep[2], tone)
                out[i] = ("beep", tone, state)
            else:
                ep[0], ep[1] = t, max(ep[1], db)
                out[i] = ("active", None, state)
        return out


# ---------------------------------------------------------------- reader

class Reader(threading.Thread):
    """
    Owns the dongle. Hops through the windows and streams chunks into a queue
    so processing never leaves gaps in the capture. Retuning only ever
    happens here.
    """

    def __init__(self, sdr, wins, q, stop, gains):
        super().__init__(daemon=True)
        self.sdr, self.wins, self.q, self.stop = sdr, wins, q, stop
        self.gains = gains                    # window index -> gain, updated by main
        self.cur_gain = None
        self.chunk = CONFIG["chunk_samples"]

    def run(self):
        sr = CONFIG["sample_rate"]
        per_dwell = dwell_chunks()
        try:
            while not self.stop.is_set():
                for w in self.wins:
                    if self.stop.is_set():
                        break
                    self.sdr.center_freq = w["centre"]
                    want = self.gains[w["i"]]
                    settle = 1
                    if want != self.cur_gain:
                        self.cur_gain = gaintest.set_gain_direct(self.sdr, want)
                        settle += 1
                    # One read per dwell, raw bytes, no conversion here. Separate
                    # small reads lost the samples streamed while each one was
                    # being converted (about a third of the time at 500 MHz).
                    n_settle = settle * CONFIG["settle_samples"]
                    n_data = per_dwell * self.chunk
                    buf = self.sdr.read_bytes(2 * (n_settle + n_data))
                    t_end = time.time()
                    # pyrtlsdr reuses this buffer on the next read: copy it.
                    raw = np.frombuffer(buf, dtype=np.uint8)[2 * n_settle:].copy()
                    self.q.put(("dwell", w["i"], t_end - n_data / sr,
                                self.cur_gain, raw))
        except Exception as exc:              # device errors surface in main
            self.q.put(("error", exc))


def dwell_chunks():
    return max(1, int(CONFIG["dwell_s"] * CONFIG["sample_rate"]
                      / CONFIG["chunk_samples"]))


_IQ_LUT = (np.arange(256, dtype=np.float32) - 127.5) / 127.5
_CLIP_LUT = (np.arange(256) <= 3) | (np.arange(256) >= 252)   # beyond +/-0.97


def bytes_to_iq(raw):
    """uint8 I/Q pairs to complex64 in [-1, 1], same scaling as pyrtlsdr."""
    iq = _IQ_LUT[raw].view(np.complex64)
    if raw.min() > 3 and raw.max() < 252:          # the usual case: no clipping
        return iq, 0
    # A sample counts as clipped if either half is; view each pair as one uint16.
    return iq, int(np.count_nonzero(_CLIP_LUT[raw].view(np.uint16)))


# ---------------------------------------------------------------- processing

class Dwell:
    def __init__(self, win, wall, gain):
        cfg = win["cfg"]
        self.win, self.wall, self.gain = win, wall, gain
        self.tracker = detector.DurationTracker(
            len(win["freqs"]), win["derived"]["bands"],
            cfg["max_gap_windows"], cfg["min_fill_ratio"])
        self.offset = 0
        self.dets = []
        self.raw = None             # uint8 I/Q bytes of the whole dwell (snippets)
        self.clipped = 0
        self.samples = 0


def chunk_maths(win, iq, hann):
    """The heavy, order-independent part: safe to run on any core."""
    cfg = win["cfg"]
    chan_db = fast_channel_db(iq, win, hann)
    trig, noise = fast_cfar(chan_db, win, cfg)
    trig[:, ~win["owned"]] = False
    trig, _ = detector.apply_bandwidth_gate(trig, cfg)
    return chan_db, trig, noise, float(np.median(chan_db))


def apply_chunk(d, chan_db, trig, noise, total, chunk_s):
    """The order-dependent part (veto history, burst tracking): main process only."""
    cfg = d.win["cfg"]
    # Veto: if the whole window lifts, CFAR's relative test means nothing.
    veto_n = max(1, int(cfg["veto_window_s"] / chunk_s))
    hist = d.win["veto"]
    if len(hist) >= veto_n:
        if total > float(np.median(hist[-veto_n:])) + cfg["veto_lift_db"]:
            trig[:, :] = False
    hist.append(total)
    del hist[:-2 * veto_n]

    # Nothing fired and no run is open: the tracker would change nothing.
    if trig.any() or d.tracker.active.any():
        d.dets.extend(d.tracker.update(trig, chan_db, noise, d.offset))
    d.offset += chan_db.shape[0]


def process_chunk(d, iq, hann):
    """Single-process path, kept for tests and tools."""
    chan_db, trig, noise, total = chunk_maths(d.win, iq, hann)
    apply_chunk(d, chan_db, trig, noise, total, len(iq) / CONFIG["sample_rate"])


# Worker processes: each builds its own copy of the windows once.
_WK = {}


def _worker_init(cfg, det_cfg):
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # Ctrl+C is the main process's job
    CONFIG.update(cfg)
    detector.CONFIG.update(det_cfg)
    _WK["wins"] = build_windows()
    _WK["hann"] = np.hanning(detector.CONFIG["fft_size"]).astype(np.float32)


def _chunk_job(wi, raw):
    t0 = time.perf_counter()
    iq, clipped = bytes_to_iq(raw)
    chan_db, trig, noise, total = chunk_maths(_WK["wins"][wi], iq, _WK["hann"])
    return chan_db, trig, noise, total, clipped, len(iq), time.perf_counter() - t0


class SnippetWriter(threading.Thread):
    """
    Writes raw IQ around a burst on its own thread. Doing this in the main
    loop stalled processing and let the queue fill.
    """

    def __init__(self, outdir):
        super().__init__(daemon=True)
        self.outdir = outdir
        self.q = queue.Queue(maxsize=10)

    def submit(self, d, det, stamp):
        fft = d.win["cfg"]["fft_size"]
        pad = int(CONFIG["snippet_pad_s"] * CONFIG["sample_rate"])
        a = max(0, det["start_window"] * fft - pad)
        b = (det["start_window"] + det["length_windows"]) * fft + pad
        name = (f"snippet_{stamp:%H%M%S}_{d.win['centre']/1e6:.4f}MHz_"
                f"{CONFIG['sample_rate']//1000}k.wav")
        try:
            self.q.put_nowait((d.raw, a, b, name))
        except queue.Full:
            return ""                      # disk too slow; skip, never block
        return name

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            raw, a, b, name = item
            try:
                iq, _ = bytes_to_iq(raw[2 * a:2 * b])
                out = np.empty(2 * len(iq), dtype=np.int16)
                out[0::2] = np.clip(iq.real * 32767, -32768, 32767)
                out[1::2] = np.clip(iq.imag * 32767, -32768, 32767)
                with wave.open(str(self.outdir / name), "wb") as w:
                    w.setnchannels(2)
                    w.setsampwidth(2)
                    w.setframerate(CONFIG["sample_rate"])
                    w.writeframes(out.tobytes())
            except Exception as exc:
                print(f"  snippet write failed: {exc}")


def lower_gain(valid, g):
    cands = [v for v in valid if v <= g - CONFIG["backoff_db"]]
    return max(cands) if cands else g


class Store:
    """
    All result files go through here. A full or failing disk costs recordings,
    never detection: write errors switch writing off and the run carries on.
    """

    def __init__(self, outdir):
        self.outdir = Path(outdir)
        self.ok = True                 # False after any write error
        self.room = True               # False when free space is below min_free_mb
        self.files = []
        try:
            self.outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(exc)
        self.check()

    def _fail(self, exc):
        if self.ok:
            print(f"WARNING: can't write results ({exc}). Detection and alerts "
                  "carry on; nothing more is saved this run.")
        self.ok = False

    def check(self):
        """Free space in MB (or None), and update whether snippets may be saved."""
        try:
            free = shutil.disk_usage(self.outdir if self.outdir.exists()
                                     else self.outdir.parent).free / 1e6
        except OSError:
            return None
        room = free >= CONFIG["min_free_mb"]
        if self.room and not room:
            print(f"WARNING: only {free:.0f} MB free: snippets off, logs continue.")
        self.room = room
        return free

    def csv(self, name, header):
        """Returns write(row). Rows are flushed so a power cut loses little."""
        fh = None
        if self.ok:
            try:
                fh = open(self.outdir / name, "w", newline="")
                self.files.append(fh)
            except OSError as exc:
                self._fail(exc)
        w = csv.writer(fh) if fh else None

        def write(row):
            nonlocal w
            if w is None or not self.ok:
                return
            try:
                w.writerow(row)
                fh.flush()
            except OSError as exc:
                self._fail(exc)
                w = None
        write(header)
        return write

    def sync(self):
        """Push buffered data onto the stick, so a power cut loses seconds, not minutes."""
        for fh in self.files:
            try:
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass

    def close(self):
        self.sync()
        for fh in self.files:
            try:
                fh.close()
            except OSError:
                pass
        self.files = []


def prune_runs(base, keep_free_mb):
    """Delete the oldest run folders until keep_free_mb is free. Newest kept."""
    try:
        runs = sorted(p for p in Path(base).iterdir()
                      if p.is_dir() and p.name.startswith("live_"))
        while len(runs) > 1 and shutil.disk_usage(base).free / 1e6 < keep_free_mb:
            old = runs.pop(0)
            shutil.rmtree(old, ignore_errors=True)
            print(f"Low on space: deleted oldest run {old.name}")
    except OSError as exc:
        print(f"Couldn't tidy old runs: {exc}")


def run_live(max_seconds=None, label=None, outdir=None):
    if label is None:
        label = input("\nLabel for this run (e.g. car-station, antenna-off): ")
    label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label.strip()) \
        or "run"
    stamp0 = datetime.now()
    outdir = Path(outdir or f"live_{label}_{stamp0:%Y%m%d_%H%M%S}")

    wins = build_windows()
    centres = ", ".join(f"{w['centre']/1e6:.3f}" for w in wins)
    print(f"\nHopping {len(wins)} windows ({centres} MHz), "
          f"{CONFIG['dwell_s']:.1f} s each, CFAR {CONFIG['cfar_offset_db']:.1f} dB, "
          f"start gain {CONFIG['gain_db']} dB.")
    print(f"Output folder: {outdir.resolve()}")
    print(f"FFT: {FFT_LIB}, numpy {np.__version__}")
    print("Ctrl+C to stop. Close other SDR programs first.\n")

    # Start worker processes before any threads exist (safer for fork).
    n_workers = CONFIG["workers"] or os.cpu_count() or 1
    pool = mp.Pool(n_workers, initializer=_worker_init,
                   initargs=(dict(CONFIG), dict(detector.CONFIG)))
    print(f"Maths on {n_workers} process(es).")

    sdr = gaintest.open_sdr(CONFIG["gain_db"])
    if sdr is None:
        pool.terminate()
        return
    # Only now make the folder, so failed starts leave nothing behind.
    store = Store(outdir)
    valid = sorted(g / 10.0 for g in sdr.gain_values)
    start_g = min(valid, key=lambda v: abs(v - CONFIG["gain_db"]))
    gains = {w["i"]: start_g for w in wins}

    beeper = Beeper() if CONFIG["beep"] else None
    link = tetra_link.Notifier()           # to ble_server.py, if it is running
    link.status(tetra_link.STATE_RUNNING)
    last_link = time.time()
    link_temp = link_busy = link_flags = None
    q = queue.Queue(maxsize=CONFIG["queue_size"])
    stop = threading.Event()
    reader = Reader(sdr, wins, q, stop, gains)
    hann = np.hanning(detector.CONFIG["fft_size"]).astype(np.float32)

    snipper = SnippetWriter(outdir)
    snipper.start()

    log_path = outdir / "detections.csv"
    log = store.csv("detections.csv",
                    ["time", "uplink_mhz", "class_ms", "duration_ms", "delta_db",
                     "window_centre_mhz", "gain_db", "snippet", "status", "frame"])
    health_log = store.csv("health.csv",
                           ["time", "temp_c", "throttled", "busy_pct", "lag_s",
                            "skipped", "free_mb", "note"])
    # Wall clock vs monotonic: spots the phone correcting the Pi's clock.
    clock_ref = (time.time(), time.monotonic())

    alerter = Alerter()
    live_sent = {}                   # channel Hz -> when strength was last sent (monotonic)
    status_counts = {}
    beep_counts = {}                 # channel Hz -> beeps
    active_counts = {}               # channel Hz -> beep + active detections
    class_counts = {}
    # Per window: when gain last changed, when it was last raised, current wait.
    gstate = {w["i"]: {"changed": time.time(), "raised": None,
                       "wait": CONFIG["recover_s"]} for w in wins}
    n_det = n_dwell = n_clipped = n_snips = n_behind = 0
    busy_hist = []
    max_temp, flags_seen = None, 0
    # "Happened" bits stick until reboot, so note what was already set: only
    # bits that are new, or the live "NOW" bits, say anything about this run.
    _, flags_before = pi_health()
    flags_before = flags_before or 0
    n_skipped = 0
    last_skip_msg = 0.0
    busy = 0.0                       # seconds spent processing since last status
    chunk_b = 2 * CONFIG["chunk_samples"]
    dwell_len_s = dwell_chunks() * CONFIG["chunk_samples"] / CONFIG["sample_rate"]
    chunk_s = CONFIG["chunk_samples"] / CONFIG["sample_rate"]
    inflight = []                    # [(Dwell, [AsyncResult per chunk])], oldest first
    max_inflight = 2                 # one being finished, one being worked on
    t_start = time.time()
    last_status = t_start
    error = None
    last_dwell_at = time.monotonic()  # watchdog: a hung dongle read never returns
    skip_at = -1e9                   # when a dwell was last skipped (monotonic)

    reader.start()
    try:
        while True:
            if max_seconds and time.time() - t_start > max_seconds:
                break
            head_ready = inflight and all(r.ready() for r in inflight[0][1])
            if inflight and (head_ready or len(inflight) > max_inflight):
                item = ("finish", inflight.pop(0))
            else:
                try:
                    item = q.get(timeout=0.05)
                except queue.Empty:
                    if time.monotonic() - last_dwell_at > CONFIG["stall_s"]:
                        error = (f"no data from the dongle for {CONFIG['stall_s']:.0f} s "
                                 "(hung, e.g. a USB power dip)")
                        print(f"  [{datetime.now():%H:%M:%S}] {error}")
                        break
                    continue
            kind = item[0]

            if kind == "error":
                error = item[1]
                break
            if kind == "dwell":
                last_dwell_at = time.monotonic()
                _, wi, wall, gain, raw = item
                lag_now = (q.qsize() + max(0, len(inflight) - 1)) * dwell_len_s
                if lag_now > CONFIG["max_lag_s"]:
                    n_skipped += 1
                    skip_at = time.monotonic()
                    if n_skipped == 1 or time.time() - last_skip_msg > 10:
                        last_skip_msg = time.time()
                        print(f"  [{datetime.now():%H:%M:%S}] {lag_now:.1f} s behind: "
                              f"skipping dwells to catch up ({n_skipped} so far). "
                              "Something is hogging the CPU.")
                    continue
                if q.qsize() > CONFIG["queue_size"] * 0.75:
                    n_behind += 1
                nd = Dwell(wins[wi], wall, gain)
                nd.raw = raw
                jobs = [pool.apply_async(_chunk_job, (wi, raw[k:k + chunk_b]))
                        for k in range(0, len(raw), chunk_b)]
                inflight.append((nd, jobs))
                continue
            if kind == "finish":
                d, jobs = item[1]
                for job in jobs:                       # in order: tracker needs it
                    chan_db, trig, noise, total, clipped, n, t_work = job.get()
                    busy += t_work
                    d.clipped += clipped
                    d.samples += n
                    apply_chunk(d, chan_db, trig, noise, total, chunk_s)
                n_dwell += 1
                d.dets.extend(d.tracker.flush())
                dets = detector.merge_adjacent(d.dets, d.win["derived"]["window_s"])
                clip_pct = 100.0 * d.clipped / max(1, d.samples)
                bad = clip_pct > CONFIG["max_clip_pct"]
                wi = d.win["i"]
                gs = gstate[wi]
                now = time.time()
                if bad:
                    n_clipped += 1
                    new = lower_gain(valid, d.gain)
                    if gs["raised"] and now - gs["raised"] < CONFIG["recover_s"]:
                        gs["wait"] = min(2 * gs["wait"], CONFIG["recover_max_s"])
                    gs["changed"], gs["raised"] = now, None
                    if new != gains[wi]:
                        print(f"  [{datetime.now():%H:%M:%S}] window "
                              f"{d.win['centre']/1e6:.3f} MHz clipped {clip_pct:.2f} %: "
                              f"gain {d.gain:.1f} -> {new:.1f} dB")
                        gains[wi] = new
                        d.win["veto"].clear()  # noise level is about to change
                elif gains[wi] < start_g and now - gs["changed"] >= gs["wait"]:
                    ups = [v for v in valid
                           if v >= gains[wi] + CONFIG["backoff_db"] and v <= start_g]
                    new = min(ups) if ups else start_g
                    print(f"  [{datetime.now():%H:%M:%S}] window "
                          f"{d.win['centre']/1e6:.3f} MHz clean for "
                          f"{gs['wait']:.0f} s: gain {gains[wi]:.1f} -> {new:.1f} dB")
                    gains[wi] = new
                    gs["changed"] = gs["raised"] = now
                    d.win["veto"].clear()
                elif gs["raised"] and now - gs["raised"] > 4 * CONFIG["recover_s"]:
                    gs["wait"], gs["raised"] = CONFIG["recover_s"], None

                w_s = d.win["derived"]["window_s"]
                rows = []
                for det in ([] if bad else dets):  # overloaded data: not trusted
                    rows.append({
                        "det": det,
                        "f": float(d.win["freqs"][det["channel_index"]]),
                        "t": d.wall + det["start_window"] * w_s,
                        "db": det["peak_delta_db"]})
                verdicts = alerter.process(rows)

                snip = ""
                for r, (status, tone, frame) in zip(rows, verdicts):
                    det, f = r["det"], r["f"]
                    when = datetime.fromtimestamp(r["t"])
                    dur = det["length_windows"] * w_s * 1000
                    cls = det["burst_class_ms"]
                    status_counts[status] = status_counts.get(status, 0) + 1
                    this_snip = ""
                    if status in ("beep", "active"):
                        n_det += 1
                        # Current strength for the phone (repeat while STRONG,
                        # "Tetra clear"). Rate-limited; never a beep by itself.
                        mono = time.monotonic()
                        if mono - live_sent.get(f, -1e9) >= tetra_link.LIVE_EVERY_S:
                            live_sent[f] = mono
                            link.live(alerter.tone_for(r["db"]), r["db"], f)
                        active_counts[f] = active_counts.get(f, 0) + 1
                        class_counts[cls] = class_counts.get(cls, 0) + 1
                    if status == "beep":
                        beep_counts[f] = beep_counts.get(f, 0) + 1
                        if (CONFIG["save_snippets"] and not snip and store.room
                                and n_snips < CONFIG["max_snippets"]):
                            snip = snipper.submit(d, det, when)
                            if snip:
                                n_snips += 1
                        this_snip = snip
                        label_ = ("weak", "medium", "STRONG")[tone]
                        print(f"ALERT {when:%H:%M:%S.%f}"[:-3] +
                              f"  {f/1e6:.4f} MHz  +{r['db']:.1f} dB  {label_:6s}"
                              f"  {dur:4.1f} ms [{cls:g}]  frame {frame or '-'}"
                              f"  gain {d.gain:.1f}")
                        if beeper:
                            beeper.beep(tone)
                        link.alert(tone, r["db"], f)
                    log([f"{when:%Y-%m-%d %H:%M:%S.%f}"[:-3],
                         f"{f/1e6:.4f}", f"{cls:g}", f"{dur:.1f}",
                         f"{r['db']:.1f}",
                         f"{d.win['centre']/1e6:.4f}", f"{d.gain:.1f}",
                         this_snip, status, frame])
                d = None

            now = time.time()
            if now - last_link >= 5.0:
                last_link = now
                behind = time.monotonic() - skip_at < CONFIG["behind_flag_s"]
                link.status(tetra_link.STATE_RUNNING, link_temp, link_busy,
                            (link_flags or 0) | (0 if store.ok and store.room else 0x10)
                            | (0x40 if behind else 0))
            if now - last_status >= CONFIG["status_every_s"]:
                # Share of all worker cores spent on the maths.
                busy_pct = 100.0 * busy / ((now - last_status) * n_workers)
                busy_hist.append(busy_pct)
                busy = 0.0
                temp, flags = pi_health()
                link_temp, link_busy = temp, busy_pct
                link_flags = (flags or 0) & 0xF
                health = ""
                if temp is not None:
                    max_temp = temp if max_temp is None else max(max_temp, temp)
                    flags_seen |= flags
                    health = f", {temp:.0f} C, throttle {flags:#x}"
                last_status = now
                free = store.check()
                store.sync()
                drift = (time.time() - clock_ref[0]) - (time.monotonic() - clock_ref[1])
                clock_ref = (time.time(), time.monotonic())
                note = f"clock moved {drift:+.0f} s" if abs(drift) > 5 else ""
                if note:
                    print(f"  [{datetime.now():%H:%M:%S}] {note} (set from the phone)")
                health_log([f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                            "" if temp is None else f"{temp:.1f}",
                            "" if flags is None else f"{flags:#x}",
                            f"{busy_pct:.0f}", f"{q.qsize() * dwell_len_s:.1f}",
                            n_skipped, "" if free is None else f"{free:.0f}", note])
                g = " ".join(f"{gains[i]:.1f}" for i in sorted(gains))
                print(f"  [{datetime.now():%H:%M:%S}] {int(now - t_start)} s, "
                      f"{n_dwell} dwells, {status_counts.get('beep', 0)} alerts, "
                      f"{status_counts.get('active', 0)} ongoing, "
                      f"{sum(status_counts.get(k, 0) for k in ('quiet', 'wideband', 'unlocked'))}"
                      f" rejected, "
                      f"{n_skipped} skipped, gains {g}, lag {q.qsize() * dwell_len_s:.1f} s, "
                      f"busy {busy_pct:.0f} %{health}")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop.set()
        # Unblock a reader waiting on a full queue, then let it finish.
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        reader.join(timeout=5)
        hung = reader.is_alive()
        snipper.q.put(None)
        snipper.join(timeout=15)
        pool.terminate()
        pool.join()
        link.status(tetra_link.STATE_DONGLE_ERROR if error is not None
                    else tetra_link.STATE_NOT_RUNNING)
        if not hung:
            try:
                sdr.close()
            except Exception:
                pass
        store.close()

    secs = time.time() - t_start
    print(f"\nRan {secs/60:.1f} min, {n_dwell} dwells, "
          f"{n_clipped} dwells overloaded, {n_snips} snippets saved.")
    print("Detections by status: " + ", ".join(
        f"{k} {v}" for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])))
    if error is not None:
        print(f"Stopped by a dongle error: {error}")
        print("pyrtlsdr closes the dongle after any failed call.")
    if n_skipped:
        print(f"Skipped {n_skipped} dwells to avoid stale beeps "
              f"(of {n_dwell + n_skipped}).")
    if n_behind:
        print(f"WARNING: processing fell behind {n_behind} times; some data "
              "may have been dropped. This machine may be too slow.")
    if class_counts:
        print("Classes: " + ", ".join(f"{k:g} ms x{v}"
                                      for k, v in sorted(class_counts.items())))
    if active_counts:
        print("\nMost active uplink channels (alerts / all radio-like detections):")
        for f, n in sorted(active_counts.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {f/1e6:.4f} MHz  {beep_counts.get(f, 0):4d} / {n}")
        act = store.csv("activity.csv", ["uplink_mhz", "alerts", "detections"])
        for f, n in sorted(active_counts.items()):
            act([f"{f/1e6:.4f}", beep_counts.get(f, 0), n])
        store.close()
    if max_temp is not None:
        print(f"Pi: max {max_temp:.0f} C, throttling flags seen: "
              f"{describe_throttle(flags_seen)}")
    print(f"\nLog: {log_path.resolve()}")
    print("If the antenna was unplugged, every alert above is a false alarm.")
    return {"dwells": n_dwell, "skipped": n_skipped, "busy": busy_hist,
            "max_temp": max_temp, "flags": flags_seen, "secs": secs,
            "flags_before": flags_before,
            "error": None if error is None else str(error), "hung": hung}


def benchmark(minutes=10):
    """Run detection with beeps and snippets off, then judge whether this
    machine keeps up. Antenna can be plugged in or not."""
    keep = (CONFIG["beep"], CONFIG["save_snippets"])
    CONFIG["beep"], CONFIG["save_snippets"] = False, False
    print(f"Benchmark: {minutes} min, beeps and snippets off. Ctrl+C ends early.")
    try:
        r = run_live(max_seconds=minutes * 60, label="bench")
    finally:
        CONFIG["beep"], CONFIG["save_snippets"] = keep
    if not r or not r["busy"]:
        print("Too short to judge.")
        return
    busy = sorted(r["busy"][1:] or r["busy"])     # first interval includes start-up
    avg, worst = sum(busy) / len(busy), busy[-1]
    full = r["secs"] / (dwell_chunks() * CONFIG["chunk_samples"] / CONFIG["sample_rate"]
                        + 0.06)
    print("\n=== Benchmark verdict ===")
    print(f"Busy: average {avg:.0f} %, worst {worst:.0f} %")
    print(f"Dwells: {r['dwells']} done, {r['skipped']} skipped "
          f"(about {full:.0f} possible at full speed)")
    if r["max_temp"] is not None:
        print(f"Temperature: max {r['max_temp']:.0f} C. Throttling: "
              f"{describe_throttle(r['flags'])}")
    before = r.get("flags_before", 0)
    flags = (r["flags"] or 0) & ~(before & 0xF0000)     # drop stale sticky bits
    if before & 0xF0000:
        print(f"(Flags already set before this run, from earlier since boot: "
              f"{describe_throttle(before & 0xF0000)}. Reboot to clear them.)")
    if flags & 0x10001:
        print("POWER: the supply sagged below what the Pi needs. Results are not "
              "trustworthy until the power is fixed.")
    if flags & 0x40004:
        print("HEAT/POWER: the Pi slowed itself down during the run.")
    if r["skipped"] == 0 and worst < 60 and not flags & 0x50005:
        print("PASS: keeps up with room to spare.")
    elif r["skipped"] <= 2 and worst < 90:
        print("MARGINAL: keeps up, but a hot car or a weak power supply could tip it over.")
    else:
        print("FAIL: too slow as it stands. Send me this output.")


# ---------------------------------------------------------------- menu

def edit_settings():
    def ask(prompt, cast, key, lo=None):
        raw = input(f"{prompt} [{CONFIG[key]}] (blank keeps): ").strip()
        if not raw:
            return
        try:
            v = cast(raw)
            CONFIG[key] = max(lo, v) if lo is not None else v
        except ValueError:
            print("Could not read that. Unchanged.")

    ask("Start gain dB", float, "gain_db")
    ask("Seconds per window", float, "dwell_s", 0.3)
    ask("CFAR offset dB", float, "cfar_offset_db", 2.0)
    raw = input(f"Beep on detections? y/n [{'y' if CONFIG['beep'] else 'n'}]: ").strip().lower()
    if raw in ("y", "n"):
        CONFIG["beep"] = raw == "y"
    zones = CONFIG["quiet_zones"]
    raw = input("Quiet zone 383.85-384.20 MHz (beeps only with TETRA timing)? y/n "
                f"[{'y' if zones else 'n'}]: ").strip().lower()
    if raw == "y":
        CONFIG["quiet_zones"] = [(383_850_000, 384_200_000)]
    elif raw == "n":
        CONFIG["quiet_zones"] = []
    raw = input(f"Save raw snippets? y/n [{'y' if CONFIG['save_snippets'] else 'n'}]: ").strip().lower()
    if raw in ("y", "n"):
        CONFIG["save_snippets"] = raw == "y"


def main():
    while True:
        print("\n=== Live TETRA uplink detector ===")
        print("1. Check the dongle can be opened")
        print("2. Start live detection")
        print("3. Settings")
        print("4. Test the beep")
        print("5. Benchmark this machine (10 min)")
        print("6. Quit")
        choice = input("> ").strip()
        if choice == "1":
            gaintest.check_environment()
        elif choice == "2":
            run_live()
        elif choice == "3":
            edit_settings()
        elif choice == "4":
            b = Beeper()
            print(f"Playing with: {b.cmd[0] if b.cmd else 'terminal bell'}")
            for k, name in enumerate(("weak (under +15 dB)", "medium (+15 to +30 dB)",
                                      "STRONG (over +30 dB)")):
                print(f"  {name}")
                b.beep(k, force=True)
                time.sleep(1.2)
        elif choice == "5":
            benchmark()
        elif choice == "6":
            return


def headless():
    """For systemd on the Pi: no menu, no beeper, runs until stopped.
    Exits non-zero on a dongle problem so systemd restarts it."""
    CONFIG["beep"] = False
    label = sys.argv[2] if len(sys.argv) > 2 else "auto"
    stamp = datetime.now()
    # The USB stick survives power cuts and the read-only card; home may not.
    base = Path("/mnt/tetra/runs")
    stick_fs = ""
    try:
        for line in open("/proc/mounts"):
            if line.split()[1] == "/mnt/tetra":
                stick_fs = line.split()[2]
    except OSError:
        pass
    if stick_fs == "overlay":
        print("WARNING: the results stick is wrapped in RAM by the card protection; "
              "results will be lost at power-off. Re-run tetra_setup.sh: 5 then 4.")
    if not (os.path.ismount("/mnt/tetra") and os.access(base, os.W_OK)):
        base = Path.home() / "runs"
        print("WARNING: USB results stick not mounted; saving to", base,
              "(lost at power-off if the card is protected).")
    prune_runs(base, CONFIG["prune_free_mb"])
    out = base / f"live_{label}_{stamp:%Y%m%d_%H%M%S}"
    r = run_live(label=label, outdir=out)
    if r is None or r.get("error"):
        tetra_link.Notifier().status(tetra_link.STATE_DONGLE_ERROR)
        sys.stdout.flush()
        if r and r.get("hung"):
            os._exit(1)              # the stuck USB read would block a normal exit
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--headless":
        headless()
    else:
        main()
