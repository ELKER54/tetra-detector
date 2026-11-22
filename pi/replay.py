#!/usr/bin/env python3
"""
replay.py - re-judge saved detections.csv logs with live.py's current rules

Every detection from a past run is fed through live.py's Alerter, so you can
see what the current version would have beeped at, without the dongle.
Works with logs from older live.py versions too.

Needs live.py (and so detector.py and gaintest.py) in the same folder.
"""

import csv
import glob
import sys
from datetime import datetime
from pathlib import Path

import live

TONE_NAMES = ("weak", "medium", "STRONG")


def load(path):
    rows = []
    # A power cut can leave zero bytes at the end of the file: drop them.
    with open(path, newline="", errors="replace") as fh:
        lines = [ln.replace("\x00", "") for ln in fh]
    for r in csv.DictReader(ln for ln in lines if ln.strip()):
        try:
            t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S.%f").timestamp()
        except (TypeError, ValueError):
            continue                       # partial line from a power cut
        rows.append({"t": t, "f": round(float(r["uplink_mhz"]) * 1e6),
                     "db": float(r["delta_db"]),
                     "win": r["window_centre_mhz"], "raw": r})
    rows.sort(key=lambda r: r["t"])
    return rows


def dwells(rows, span_s=0.85):
    """Group detections back into dwells: same window, within one dwell's time."""
    out, cur = [], []
    for r in rows:
        if cur and (r["win"] != cur[0]["win"] or r["t"] - cur[0]["t"] > span_s):
            out.append(cur)
            cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


def replay(path, show=True):
    rows = load(path)
    if not rows:
        print(f"{path}: empty")
        return None
    al = live.Alerter()
    counts, tones, alerts = {}, [0, 0, 0], []
    for dw in dwells(rows):
        for r, (status, tone, frame) in zip(dw, al.process(dw)):
            counts[status] = counts.get(status, 0) + 1
            if status == "beep":
                tones[tone] += 1
                alerts.append((r, tone, frame))
    mins = max((rows[-1]["t"] - rows[0]["t"]) / 60.0, 0.1)
    name = Path(path).parent.name
    print(f"\n{name}: {mins:.1f} min, {len(rows)} detections")
    print(f"  alerts {len(alerts)} ({len(alerts) / mins:.1f}/min): "
          f"weak {tones[0]}, medium {tones[1]}, STRONG {tones[2]}")
    print("  status: " + ", ".join(f"{k} {v}" for k, v in
                                    sorted(counts.items(), key=lambda kv: -kv[1])))
    if show:
        for r, tone, frame in alerts:
            print(f"    {datetime.fromtimestamp(r['t']):%H:%M:%S}  "
                  f"{r['f'] / 1e6:.4f} MHz  +{r['db']:5.1f} dB  "
                  f"{TONE_NAMES[tone]:6s}  frame {frame or '-'}")
    return counts


def main():
    while True:
        print("\n=== Replay saved runs ===")
        print("1. Replay every live_* folder here (summary only)")
        print("2. Replay one folder (list every alert)")
        print("3. Quit")
        c = input("> ").strip()
        if c == "1":
            for p in sorted(glob.glob("live_*/detections.csv")):
                replay(p, show=False)
        elif c == "2":
            folders = sorted(glob.glob("live_*/detections.csv"))
            for i, p in enumerate(folders, 1):
                print(f"  {i}. {Path(p).parent.name}")
            try:
                replay(folders[int(input("Number: ")) - 1])
            except (ValueError, IndexError):
                print("Not a valid number.")
        elif c == "3":
            return


if __name__ == "__main__":
    if len(sys.argv) > 1:                  # scripted use: replay.py path/detections.csv
        for p in sys.argv[1:]:
            replay(p)
    else:
        main()
