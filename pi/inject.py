#!/usr/bin/env python3
"""
inject.py - build a positive-control test file

Takes a short extract from a real baseband recording, adds synthetic TETRA-like
bursts at a range of known amplitudes above the measured noise floor, and writes
a small WAV you can run detector.py against.

This is the only way to measure sensitivity. A clean run on an empty recording
proves the detector doesn't fire on noise; it says nothing about whether it
would catch a real burst.

Usage:
    python inject.py baseline.wav
    python inject.py baseline.wav --seconds 30 --offset 120

Then:
    python detector.py injected.wav --cfar 6

The printed table tells you which burst is at which time and level. Compare
against what detector.py reports: the weakest one it finds is your detection
floor in dB above noise.
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

# Bursts are placed one per slot, spaced out in time, descending in strength.
# Levels are dB above the measured per-channel noise floor of the extract.
DEFAULT_LEVELS_DB = [25, 20, 16, 13, 10, 8, 6, 4, 2]

# On-air ACTIVE burst durations, not slot allocations. See detector.py CONFIG.
BURST_MS = 12.833           # normal uplink, 231 symbols
SUBSLOT_MS = 5.722          # control uplink / random access, 103 symbols
RAMP_MS = 0.944             # 34 bits of ramp-up and PA linearisation
CHANNEL_WIDTH_HZ = 25_000       # raster spacing, used for level reference
OCCUPIED_BW_HZ = 18_000         # pi/4-DQPSK at 18 ksym/s actually occupies
                                # about 18 kHz, not the full channel. Bursts
                                # built 25 kHz wide made the positive controls
                                # wider than real TETRA and understated what
                                # a matched integration window buys.
RASTER_BASE_HZ = 380_012_500
RASTER_STEP_HZ = 25_000


def read_extract(path, offset_s, seconds):
    """Read `seconds` of IQ starting `offset_s` into the file."""
    wf = wave.open(str(path), "rb")
    if wf.getnchannels() != 2:
        sys.exit("Expected a 2-channel baseband recording.")

    sr = wf.getframerate()
    sw = wf.getsampwidth()
    total = wf.getnframes()

    start = int(offset_s * sr)
    want = int(seconds * sr)
    if start + want > total:
        sys.exit(f"Requested {offset_s}s + {seconds}s exceeds the "
                 f"{total/sr:.1f}s file.")

    wf.setpos(start)
    raw = wf.readframes(want)
    wf.close()

    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        sys.exit(f"Unsupported sample width {sw}.")

    iq = (data[0::2] + 1j * data[1::2]).astype(np.complex64)
    return iq, sr, sw


def channel_block_powers(iq, sr, fft_size=2048):
    """
    Integrated power in each 25 kHz-wide block, using the same windowing and
    integration the detector uses, so injected levels mean what they say.
    """
    n = len(iq) // fft_size
    if n == 0:
        return np.array([])
    batch = iq[:n * fft_size].reshape(n, fft_size)
    spec = np.fft.fftshift(np.fft.fft(batch * np.hanning(fft_size), axis=1), axes=1)
    power = np.abs(spec) ** 2

    bins_per_channel = max(1, int(round(CHANNEL_WIDTH_HZ / (sr / fft_size))))
    usable = (fft_size // bins_per_channel) * bins_per_channel
    return power[:, :usable].reshape(n, -1, bins_per_channel).sum(axis=2)


def measure_channel_noise(iq, sr, fft_size=2048):
    """Median 25 kHz-block power of the extract: the reference noise floor."""
    blocks = channel_block_powers(iq, sr, fft_size)
    return float(np.median(blocks))


def make_burst(n_samples, sr, rng, fft_size=2048):
    """
    Noise-like signal band-limited to 25 kHz, scaled so its integrated channel
    power is exactly 1.0. Not real pi/4-DQPSK, but it has the right bandwidth
    and flat-topped spectrum, which is what the detector's gates examine.

    Band-limiting is done in the frequency domain: generate white noise, zero
    every bin outside the channel, transform back.
    """
    pad = max(n_samples, fft_size * 2)
    raw = rng.normal(0, 1, pad) + 1j * rng.normal(0, 1, pad)

    spec = np.fft.fft(raw)
    freqs = np.fft.fftfreq(pad, d=1.0 / sr)
    spec[np.abs(freqs) > OCCUPIED_BW_HZ / 2] = 0
    band = np.fft.ifft(spec)[:n_samples].astype(np.complex64)

    # Normalise empirically against the same measurement the detector makes,
    # rather than assuming what the filtering did to the power.
    blocks = channel_block_powers(band, sr, fft_size)
    if blocks.size == 0:
        return band
    unit_power = float(blocks.max())
    if unit_power <= 0:
        return band
    return (band / np.sqrt(unit_power)).astype(np.complex64)


def apply_ramp(burst, sr):
    """
    Raised-cosine ramp at each end, approximating the 34 bits of transmitter
    ramp-up and PA linearisation. Without it the injected burst has square
    edges that a real one does not, which flatters any duration measurement.
    """
    n = len(burst)
    r = min(int(RAMP_MS / 1000.0 * sr), n // 3)
    if r < 2:
        return burst
    env = np.ones(n, dtype=np.float32)
    rise = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
    env[:r] = rise
    env[-r:] = rise[::-1]
    return (burst * env).astype(np.complex64)


_FADE_CACHE = {}


def _fading_sequence(sr, fd, pad=1 << 19):
    """
    One long Rayleigh envelope, normalised over its whole length and cached.

    Generating a short envelope per burst and normalising it per burst - which
    an earlier version did - divides the fade back out. The histogram of
    per-burst mean power then collapses to a spike at 0 dB and fading has no
    effect at all. Slices of one long normalised sequence keep the
    burst-to-burst level variation, which for a burst shorter than the
    coherence time is the whole effect.
    """
    key = (sr, round(fd, 3), pad)
    if key not in _FADE_CACHE:
        rng = np.random.default_rng(0xFADE)
        g = rng.normal(0, 1, pad) + 1j * rng.normal(0, 1, pad)
        spec = np.fft.fft(g)
        freqs = np.fft.fftfreq(pad, d=1.0 / sr)
        spec[np.abs(freqs) > fd] = 0
        env = np.fft.ifft(spec)
        env /= np.sqrt(np.mean(np.abs(env) ** 2))   # over the WHOLE sequence
        _FADE_CACHE[key] = env.astype(np.complex64)
    return _FADE_CACHE[key]


def apply_fading(burst, sr, rng, speed_mps=31.3, carrier_hz=382.5e6):
    """
    Multiply the burst by a slice of a long Rayleigh envelope. Doppler at
    70 mph and 380 MHz is about 40 Hz, giving a coherence time near 11 ms.

    A 5.72 ms control burst is shorter than that, so it sees a roughly
    constant but randomly scaled envelope: fading varies the level between
    bursts rather than punching a hole inside one. A 12.83 ms normal burst is
    longer than the coherence time and does see intra-burst variation.
    """
    n = len(burst)
    fd = speed_mps / (3e8 / carrier_hz)
    env = _fading_sequence(sr, fd)
    off = int(rng.integers(0, len(env) - n))
    return (burst * env[off:off + n]).astype(np.complex64)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav", type=Path, help="real baseline recording to extract from")
    ap.add_argument("--out", type=Path, default=Path("injected.wav"))
    ap.add_argument("--offset", type=float, default=60.0,
                    help="seconds into the source file to start the extract")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="length of extract to build")
    ap.add_argument("--centre", type=float, default=382.5e6,
                    help="centre frequency of the recording in Hz")
    ap.add_argument("--levels", type=float, nargs="+", default=DEFAULT_LEVELS_DB,
                    help="burst levels in dB above the channel noise floor")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--half-slot", action="store_true",
                    help="plant 7.083 ms subslot bursts (Random Access / "
                         "Control Uplink) instead of full 14.167 ms slots")
    ap.add_argument("--fade", action="store_true",
                    help="apply Rayleigh-ish fading to each burst, "
                         "approximating vehicle speed at 380 MHz")
    args = ap.parse_args()

    if not args.wav.exists():
        sys.exit(f"File not found: {args.wav}")

    rng = np.random.default_rng(args.seed)
    iq, sr, sw = read_extract(args.wav, args.offset, args.seconds)
    noise_power = measure_channel_noise(iq, sr)

    burst_ms = SUBSLOT_MS if args.half_slot else BURST_MS
    burst_samples = int(burst_ms / 1000.0 * sr)
    spacing_s = args.seconds / (len(args.levels) + 1)

    # Pick raster channels well away from the DC spike and the band edges.
    usable_half = sr * 0.35
    candidates = []
    n = int(np.ceil((args.centre - usable_half - RASTER_BASE_HZ) / RASTER_STEP_HZ))
    while True:
        f = RASTER_BASE_HZ + n * RASTER_STEP_HZ
        if f > args.centre + usable_half:
            break
        if abs(f - args.centre) > 100_000:      # keep clear of LO leakage
            candidates.append(f)
        n += 1

    if len(candidates) < len(args.levels):
        sys.exit("Not enough usable raster channels for that many levels.")

    # Spread the chosen channels across the band rather than bunching them.
    step = len(candidates) // len(args.levels)
    chosen = [candidates[i * step] for i in range(len(args.levels))]

    print(f"Source:      {args.wav.name}")
    print(f"Extract:     {args.seconds:.0f}s from {args.offset:.0f}s in")
    print(f"Sample rate: {sr/1e6:.3f} MS/s")
    print(f"Burst:       {burst_ms:.3f} ms"
          f"{' (half slot)' if args.half_slot else ''}, "
          f"{CHANNEL_WIDTH_HZ/1000:.0f} kHz wide"
          f"{', faded' if args.fade else ''}")
    print()
    print(f"{'time (s)':>9}  {'frequency':>12}  {'level':>9}")

    for i, (level_db, freq) in enumerate(zip(args.levels, chosen)):
        t0 = spacing_s * (i + 1)
        start = int(t0 * sr)
        end = start + burst_samples
        if end > len(iq):
            break

        burst = apply_ramp(make_burst(burst_samples, sr, rng), sr)
        if args.fade:
            burst = apply_fading(burst, sr, rng)

        # burst already has unit integrated channel power, so scaling to a
        # target level is a single multiply.
        target_power = noise_power * (10.0 ** (level_db / 10.0))
        scale = np.sqrt(target_power)

        t = np.arange(burst_samples) / sr
        shifted = burst * np.exp(2j * np.pi * (freq - args.centre) * t)
        iq[start:end] += (shifted * scale).astype(np.complex64)

        print(f"{t0:9.3f}  {freq/1e6:9.4f} MHz  {level_db:6.1f} dB")

    peak = np.max(np.abs(iq))
    if peak > 0.99:
        print(f"\nClipping avoided: scaling output by {0.99/peak:.3f}")
        iq *= 0.99 / peak

    out = np.empty(2 * len(iq), dtype=np.int16)
    out[0::2] = np.clip(iq.real * 32767, -32768, 32767)
    out[1::2] = np.clip(iq.imag * 32767, -32768, 32767)

    w = wave.open(str(args.out), "wb")
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(out.tobytes())
    w.close()

    print()
    print(f"Written {args.out} ({args.out.stat().st_size/1e6:.0f} MB)")
    print()
    print(f"Now run:  python detector.py {args.out} --cfar 6")
    print("The weakest level it still finds is your detection floor.")


if __name__ == "__main__":
    main()
