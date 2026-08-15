#!/usr/bin/env python3
"""
decode_log.py - Her-analyseer een opgeslagen K-line log (ruwe hex).

Werkt op zowel de logs die mt03_kline.py wegschrijft als op vrije-vorm
hex-dumps (zoals de originele 'ECU Log.txt'): niet-hex regels en labels
worden genegeerd. Alle hex-bytes worden als een stream gelezen en op
checksum gesynchroniseerd, zodat ook verkeerd-geframede logs kloppen.

Gebruik:
    python3 decode_log.py ECU_Log.txt
    python3 decode_log.py log/kline_..._raw.txt --temp-offset -30 --csv uit.csv
"""

import argparse
import re
import sys
from collections import Counter

from kline_protocol import decode_frame, error_flags, FAULT_CODES, frames_from_stream

HEXTOKEN = re.compile(r"^[0-9a-fA-F]{2}$")


def load_frames(path):
    """
    Lees ALLE hex-bytes uit het bestand als één stream (regelgrenzen en labels
    worden genegeerd) en synchroniseer op checksum. Dit herstelt logs die door
    de oude gap-framing verkeerd waren uitgelijnd.
    """
    data = bytearray()
    with open(path) as f:
        for line in f:
            for tok in line.split():
                if HEXTOKEN.match(tok):
                    data.append(int(tok, 16))
    return frames_from_stream(bytes(data))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Decodeer een K-line hex-log.")
    ap.add_argument("logfile")
    ap.add_argument("--temp-offset", type=int, default=0)
    ap.add_argument("--fault-encoding", default="auto",
                    choices=["auto", "bcd", "decimal", "raw"],
                    help="hoe het foutcode-byte wordt gelezen (default auto)")
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
    fault_hist = Counter()          # code -> aantal
    temp_open_cnt = 0
    rpm_min = None
    rpm_max = None
    rows = []

    for fr in frames:
        d = decode_frame(fr, temp_offset=args.temp_offset,
                         fault_encoding=args.fault_encoding)
        if d.kind in ("data5", "data6"):
            data += 1
            if d.checksum_ok:
                ok += 1
            else:
                bad += 1
            err_hist[d.error] += 1
            if d.fault_code is not None:
                fault_hist[d.fault_code] += 1
            if d.temp_open:
                temp_open_cnt += 1
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

        if temp_open_cnt:
            pct = 100 * temp_open_cnt / data
            print(f"\n>> Koelvloeistofsensor: temp=0xff (OPEN CIRCUIT) in "
                  f"{temp_open_cnt}/{data} frames ({pct:.0f}%) "
                  f"-> Yamaha-foutcode 21.")

        # Drempel: een echte, actieve storing is persistent. Losse matches
        # komen vaak uit immobilizer-handshake bytes die toevallig uitlijnen.
        threshold = max(10, int(0.01 * data))
        persistent = {c: n for c, n in fault_hist.items() if n >= threshold}
        incidental = {c: n for c, n in fault_hist.items() if n < threshold}

        print("\n>> Herkende Yamaha-foutcodes:")
        if persistent:
            for code, cnt in sorted(persistent.items()):
                print(f"    FOUT {code}: {FAULT_CODES.get(code, '?')}  ({cnt}x)")
        else:
            print("    geen persistente foutcodes in het error-byte")
        if incidental:
            det = ", ".join(f"{c} ({n}x)" for c, n in sorted(incidental.items()))
            print(f"    (incidenteel, waarschijnlijk immo-handshake ruis: {det})")

    if args.show and rows:
        print(f"\nEerste {min(args.show, len(rows))} dataframes:")
        for d in rows[:args.show]:
            fault = f"  << FOUT {d.fault_code}: {d.fault_desc}" if d.fault_code else ""
            print(f"  {d.hex():20s} RPM {d.rpm:5d}  vel {d.velocity:3d}  "
                  f"err 0x{d.error:02x}  temp {d.temp_c:3d}  "
                  f"chk {'ok' if d.checksum_ok else 'BAD'}{fault}")

    if args.csv:
        with open(args.csv, "w") as out:
            out.write("idx,len,kind,checksum,rpm,velocity,error_hex,temp_raw,"
                      "temp_c,fault_code,fault_desc\n")
            for i, d in enumerate(rows):
                fc = d.fault_code if d.fault_code is not None else ""
                fd = f"\"{d.fault_desc}\"" if d.fault_desc else ""
                out.write(f"{i},{d.length},{d.kind},"
                          f"{'ok' if d.checksum_ok else 'bad'},{d.rpm},"
                          f"{d.velocity},0x{d.error:02x},{d.temp_raw},{d.temp_c},"
                          f"{fc},{fd}\n")
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
