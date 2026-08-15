#!/usr/bin/env python3
"""
decode_log.py - Her-analyseer een opgeslagen K-line log (ruwe hex).

Werkt op zowel de logs die mt03_kline.py wegschrijft als op vrije-vorm
hex-dumps (zoals de originele 'ECU Log.txt'): niet-hex regels en labels
worden genegeerd.

Gebruik:
    python3 decode_log.py ECU_Log.txt
    python3 decode_log.py log/kline_..._raw.txt --temp-offset -30 --csv uit.csv
"""

import argparse
import re
import sys
from collections import Counter

from kline_protocol import decode_frame, error_flags

HEXLINE = re.compile(r"^\s*([0-9a-fA-F]{2})(\s+[0-9a-fA-F]{2})*\s*$")


def load_frames(path):
    frames = []
    with open(path) as f:
        for line in f:
            if HEXLINE.match(line):
                frames.append(bytes(int(b, 16) for b in line.split()))
    return frames


def main(argv=None):
    ap = argparse.ArgumentParser(description="Decodeer een K-line hex-log.")
    ap.add_argument("logfile")
    ap.add_argument("--temp-offset", type=int, default=0)
    ap.add_argument("--csv", help="schrijf gedecodeerde dataframes naar CSV")
    ap.add_argument("--show", type=int, default=20,
                    help="aantal dataframes om te tonen (0 = alle)")
    args = ap.parse_args(argv)

    frames = load_frames(args.logfile)
    if not frames:
        sys.exit("Geen hex-frames gevonden in dit bestand.")

    lens = Counter(len(f) for f in frames)
    ok = bad = data = 0
    err_hist = Counter()
    rpm_min = None
    rpm_max = None
    rows = []

    for fr in frames:
        d = decode_frame(fr, temp_offset=args.temp_offset)
        if d.kind in ("data5", "data6"):
            data += 1
            if d.checksum_ok:
                ok += 1
            else:
                bad += 1
            err_hist[d.error] += 1
            rpm_min = d.rpm if rpm_min is None else min(rpm_min, d.rpm)
            rpm_max = d.rpm if rpm_max is None else max(rpm_max, d.rpm)
            rows.append(d)

    print(f"Bestand        : {args.logfile}")
    print(f"Frames totaal  : {len(frames)}  (lengtes: {dict(sorted(lens.items()))})")
    print(f"Dataframes     : {data}  (checksum OK {ok}, BAD {bad})")
    if data:
        print(f"RPM-bereik     : {rpm_min} - {rpm_max}")
        print("Error-byte histogram (waarde: aantal):")
        for val, cnt in err_hist.most_common():
            flags = " ".join(error_flags(val))
            print(f"    0x{val:02x} : {cnt:5d}   [{flags}]")

    if args.show and rows:
        print(f"\nEerste {min(args.show, len(rows))} dataframes:")
        for d in rows[:args.show]:
            print(f"  {d.hex():20s} RPM {d.rpm:5d}  vel {d.velocity:3d}  "
                  f"err 0x{d.error:02x}  temp {d.temp_c:3d}  "
                  f"chk {'ok' if d.checksum_ok else 'BAD'}")

    if args.csv:
        with open(args.csv, "w") as out:
            out.write("idx,len,kind,checksum,rpm,velocity,error_hex,temp_raw,temp_c\n")
            for i, d in enumerate(rows):
                out.write(f"{i},{d.length},{d.kind},"
                          f"{'ok' if d.checksum_ok else 'bad'},{d.rpm},"
                          f"{d.velocity},0x{d.error:02x},{d.temp_raw},{d.temp_c}\n")
        print(f"\nCSV geschreven: {args.csv}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # netjes afsluiten bij pipen naar head/less
        try:
            sys.stdout.close()
        except Exception:
            pass
