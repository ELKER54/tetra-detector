#!/usr/bin/env python3
"""
scanner.py

Sweeps the UK Airwave TETRA uplink band (380-385 MHz, vehicle/handset
transmit) using rtl_power, then reports the strongest frequencies found.

Run this first to find your local mast's actual uplink channel(s) before
hardcoding a frequency into detector.py - exact channel allocation varies
by site.

Requires: rtl-sdr package (provides rtl_power)
    sudo apt install rtl-sdr
"""

import subprocess
import csv
import sys
import argparse
from pathlib import Path

# TETRA channels are spaced 25 kHz apart (12.5 kHz raster also used in
# some deployments). 12.5 kHz bins give us enough resolution to see
# individual channels without an excessive scan time.
BIN_SIZE_HZ = 12500
LOW_FREQ = "380M"
HIGH_FREQ = "385M"


def run_scan(duration_seconds: int, output_csv: Path) -> None:
    """Run rtl_power for the given duration, writing raw results to CSV."""
    cmd = [
        "rtl_power",
        "-f", f"{LOW_FREQ}:{HIGH_FREQ}:{BIN_SIZE_HZ}",
        "-i", "1",                     # 1 second integration per sweep
        "-e", f"{duration_seconds}s",  # total run time
        str(output_csv),
    ]
    print(f"Scanning {LOW_FREQ}-{HIGH_FREQ} for {duration_seconds}s ...")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.exit("rtl_power not found. Install it with: sudo apt install rtl-sdr")
    except subprocess.CalledProcessError as e:
        sys.exit(f"rtl_power failed: {e}")


def parse_peaks(output_csv: Path, top_n: int) -> list[tuple[float, float]]:
    """
    Parse rtl_power's CSV output and return the top_n strongest
    (frequency_hz, power_db) readings, averaged across all sweeps.

    rtl_power CSV format per row:
    date, time, low_hz, high_hz, bin_width_hz, samples, dB, dB, dB, ...
    Each row is one sweep; each dB value after the fixed columns
    corresponds to one frequency bin.
    """
    # Accumulate readings per bin frequency across every sweep so a
    # single noisy spike doesn't look like a strong channel.
    totals: dict[float, list[float]] = {}

    with open(output_csv, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            low_hz = float(row[2])
            bin_width_hz = float(row[4])
            db_values = [float(v) for v in row[6:] if v.strip()]

            for i, db in enumerate(db_values):
                freq = low_hz + (i * bin_width_hz)
                totals.setdefault(freq, []).append(db)

    if not totals:
        sys.exit("No data parsed from rtl_power output - check the CSV file.")

    # Average each bin's readings, then sort strongest first.
    averaged = [(freq, sum(vals) / len(vals)) for freq, vals in totals.items()]
    averaged.sort(key=lambda x: x[1], reverse=True)

    return averaged[:top_n]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=int, default=60,
        help="scan duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="number of strongest frequencies to report (default: 10)",
    )
    parser.add_argument(
        "--csv", type=Path, default=Path("scan_output.csv"),
        help="path to save raw rtl_power CSV output",
    )
    args = parser.parse_args()

    run_scan(args.duration, args.csv)
    peaks = parse_peaks(args.csv, args.top)

    print(f"\nTop {args.top} strongest frequencies (380-385 MHz):\n")
    print(f"{'Frequency (MHz)':>18}  {'Power (dB)':>10}")
    for freq_hz, db in peaks:
        print(f"{freq_hz / 1e6:>18.4f}  {db:>10.2f}")

    print(
        "\nNote: a strong reading here just means there's RF energy at that "
        "frequency during the scan window. It won't tell you whether it's "
        "TETRA specifically - cross-check with GQRX to listen/view the "
        "waterfall before committing detector.py to a frequency."
    )


if __name__ == "__main__":
    main()
