#!/usr/bin/env python3
"""
harness.py - in-memory statistical test rig for detector.py

Streams generated IQ straight through the detection chain without writing
WAV files, so hours of noise can be tested in minutes and sensitivity can be
measured over many trials per level instead of one.

Not part of the deliverable. Used to produce the figures in plan rev 5.
"""

import numpy as np
import detector as D
import inject as I


def make_ctx(cfg, sr):
    derived = D.derive_params(cfg, sr)
    chan_freqs, bin_lists, _ = D.build_channel_map(cfg, sr, derived)
    return {
        "cfg": cfg, "sr": sr, "derived": derived,
        "chan_freqs": chan_freqs, "bin_lists": bin_lists,
        "hann": np.hanning(cfg["fft_size"]).astype(np.float32),
    }


def run_stream(ctx, chunk_iter):
    """Run the full chain over an iterable of complex64 chunks."""
    cfg, derived = ctx["cfg"], ctx["derived"]
    fft_size = cfg["fft_size"]
    tracker = D.DurationTracker(len(ctx["chan_freqs"]), derived["bands"],
                                cfg["max_gap_windows"], cfg["min_fill_ratio"])
    dets, windows = [], 0
    veto_hist = []
    veto_batches = max(1, int(cfg["veto_window_s"] /
                              (derived["window_s"] * cfg["windows_per_batch"])))

    for iq in chunk_iter:
        if len(iq) < fft_size:
            continue
        chan_db = D.channel_powers_db(iq, fft_size, ctx["bin_lists"], ctx["hann"])
        trig, noise_db = D.cfar_trigger(chan_db, cfg)
        trig, _ = D.apply_bandwidth_gate(trig, cfg)

        tot = float(np.median(chan_db))
        if len(veto_hist) >= veto_batches:
            if tot > float(np.median(veto_hist[-veto_batches:])) + cfg["veto_lift_db"]:
                trig[:, :] = False
        veto_hist.append(tot)

        dets.extend(tracker.update(trig, chan_db, noise_db, windows))
        windows += chan_db.shape[0]

    dets.extend(tracker.flush())
    return D.merge_adjacent(dets, derived["window_s"]), windows


def noise_chunks(sr, seconds, batch_samples, rng, sigma=0.01):
    """Generate Gaussian noise in batch-sized pieces, never held whole."""
    total = int(sr * seconds)
    done = 0
    while done < total:
        n = min(batch_samples, total - done)
        yield (rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)).astype(np.complex64)
        done += n


def burst_trial_chunks(ctx, rng, level_db, n_trials, spacing_s,
                       half_slot=True, fade=False, sigma=0.01):
    """
    Noise with one burst per `spacing_s`, all at the same level. Yields
    batch-sized chunks and the list of planted (time_s, freq_hz).
    """
    cfg, sr = ctx["cfg"], ctx["sr"]
    batch = cfg["fft_size"] * cfg["windows_per_batch"]
    centre = cfg["centre_freq_hz"]

    ms = I.SUBSLOT_MS if half_slot else I.BURST_MS
    bs = int(ms / 1000.0 * sr)

    # Reference noise power in one integration width, to define level.
    probe = (rng.normal(0, sigma, 1 << 18) + 1j * rng.normal(0, sigma, 1 << 18)).astype(np.complex64)
    npow = float(np.median(D.channel_powers_db(probe, cfg["fft_size"],
                                               ctx["bin_lists"], ctx["hann"])))
    target_lin = 10 ** ((npow + level_db) / 10.0)

    # Spread trials across channels away from DC and the band edges.
    cf = ctx["chan_freqs"]
    usable = [f for f in cf if abs(f - centre) > 150_000]
    planted = []

    total = int(sr * spacing_s * n_trials)
    done = 0
    pending = []
    for k in range(n_trials):
        t0 = spacing_s * (k + 0.5)
        f = usable[k % len(usable)]
        pending.append((int(t0 * sr), f))
        planted.append((t0, f))

    pi = 0
    # A burst can straddle a chunk boundary. An earlier version skipped those
    # while still counting them as planted, which put a hard ceiling of about
    # 0.85 on every Pd curve regardless of level. Carry the remainder forward.
    carry = np.zeros(0, dtype=np.complex64)

    while done < total:
        n = min(batch, total - done)
        chunk = (rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)).astype(np.complex64)

        if len(carry):
            m = min(len(carry), n)
            chunk[:m] += carry[:m]
            carry = carry[m:]

        while pi < len(pending) and pending[pi][0] < done + n:
            s0, f = pending[pi]
            off = s0 - done
            b = I.apply_ramp(I.make_burst(bs, sr, rng), sr)
            if fade:
                b = I.apply_fading(b, sr, rng)
            bp = D.channel_powers_db(b, cfg["fft_size"], ctx["bin_lists"], ctx["hann"])
            unit = 10 ** (float(np.max(bp)) / 10.0) if bp.size else 1.0
            scale = np.sqrt(target_lin / max(unit, 1e-30))
            t = np.arange(bs) / sr
            full = (b * scale * np.exp(2j * np.pi * (f - centre) * t)).astype(np.complex64)

            take = max(0, min(bs, n - off))
            if take > 0:
                chunk[off:off + take] += full[:take]
            if take < bs:
                rem = full[take:]
                if len(carry) < len(rem):
                    carry = np.concatenate(
                        [carry, np.zeros(len(rem) - len(carry), dtype=np.complex64)])
                carry[:len(rem)] += rem
            pi += 1

        yield chunk
        done += n

    return planted


def score(dets, planted, ctx, tol_s=0.02, tol_hz=30_000):
    """run_stream returns raw tracker dicts; convert to time/freq here."""
    ws = ctx["derived"]["window_s"]
    cf = ctx["chan_freqs"]
    conv = [(d["start_window"] * ws, float(cf[d["channel_index"]])) for d in dets]
    hit = 0
    for t0, f in planted:
        if any(abs(dt - t0) < tol_s and abs(df - f) < tol_hz for dt, df in conv):
            hit += 1
    return hit
