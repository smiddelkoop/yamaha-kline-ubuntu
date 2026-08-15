#!/usr/bin/env python3
"""
mt03_kline.py - Live Yamaha K-line ECU reader voor Ubuntu / Linux.

Leest passief mee op de K-line van een pre-2015 Yamaha (o.a. MT-03 660 2010)
via een KKL 409.1 USB-kabel (FTDI FT232RL) op de 3-pins diagnosestekker.

Gebruik:
    python3 mt03_kline.py                     # auto-detecteert /dev/ttyUSB*
    python3 mt03_kline.py -p /dev/ttyUSB0     # expliciete poort
    python3 mt03_kline.py --temp-offset -30   # temp-kalibratie toepassen
    python3 mt03_kline.py --raw               # toon ook ruwe hex van elk frame

Toetsen tijdens draaien:
    spatie = pauzeer/hervat loggen
    q      = stoppen   (Ctrl-C werkt ook)

Alle frames worden weggeschreven naar log/ met een tijdstempel:
    log/kline_YYYYmmdd_HHMMSS_raw.txt      (alle frames, ruwe hex)
    log/kline_YYYYmmdd_HHMMSS_decoded.csv  (gedecodeerde dataframes)

Gebaseerd op het protocol uit terrafirma2021/Yamaha-K-line-* (Arduino/Windows),
herschreven voor Python 3 + Linux, met checksum-synchronisatie en decodering.
"""

import argparse
import glob
import os
import select
import signal
import sys
import termios
import time
import tty
from datetime import datetime

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:
    sys.exit(
        "pyserial ontbreekt. Installeer met:\n"
        "    pip3 install pyserial\n"
        "of:  sudo apt install python3-serial"
    )

from kline_protocol import decode_frame, error_flags, FAULT_CODES, FrameSync

DEFAULT_BAUD = 16064          # non-standaard Yamaha K-line baudrate
READ_TIMEOUT = 0.02          # blokkeer-timeout per read()


# ---------------------------------------------------------------- kleuren ----
class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"

    @classmethod
    def disable(cls):
        for name in ("RESET", "DIM", "RED", "GREEN", "YELLOW", "CYAN", "BOLD"):
            setattr(cls, name, "")


# ------------------------------------------------------------ poortkeuze ----
def find_port(explicit):
    if explicit:
        return explicit
    # FTDI-kabels verschijnen als /dev/ttyUSB*
    candidates = []
    for p in list_ports.comports():
        desc = f"{p.description} {p.manufacturer or ''} {p.product or ''}".lower()
        if "ftdi" in desc or "ft232" in desc or "usb" in desc:
            candidates.append(p.device)
    if not candidates:
        candidates = sorted(glob.glob("/dev/ttyUSB*"))
    if not candidates:
        sys.exit(
            "Geen seriële poort gevonden. Sluit de KKL-kabel aan en controleer:\n"
            "    ls -l /dev/ttyUSB*\n"
            "of geef de poort expliciet op met -p /dev/ttyUSB0"
        )
    if len(candidates) > 1:
        print(f"{C.YELLOW}Meerdere poorten gevonden: {candidates}. "
              f"Kies de eerste ({candidates[0]}) of geef -p op.{C.RESET}")
    return candidates[0]


def open_serial(port, baud):
    """
    Open de poort op een (mogelijk non-standaard) baudrate.

    16064 baud is niet-standaard. pyserial zet dit op Linux via de FTDI-driver
    (BOTHER/aliasing). Werkt dit niet, dan is er een handmatige fallback nodig
    (zie README, sectie 'Non-standaard baudrate').
    """
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=READ_TIMEOUT,
        )
        return ser
    except (serial.SerialException, ValueError) as e:
        sys.exit(
            f"Kon poort {port} niet openen op {baud} baud: {e}\n"
            "Controleer rechten (sudo usermod -aG dialout $USER; opnieuw inloggen)\n"
            "of de baudrate-ondersteuning (zie README)."
        )


# ------------------------------------------------------ toetsenbord (POSIX) --
class RawKeyboard:
    """Non-blocking losse toetsen lezen zonder Enter (Linux/macOS)."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.enabled = sys.stdin.isatty()
        self.old = None

    def __enter__(self):
        if self.enabled:
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *a):
        if self.enabled and self.old:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def getch(self):
        if not self.enabled:
            return None
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None


# ------------------------------------------------------------- hoofdlus -----
def run(args):
    if args.no_color or not sys.stdout.isatty():
        C.disable()

    port = find_port(args.port)
    ser = open_serial(port, args.baud)

    os.makedirs(args.logdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = os.path.join(args.logdir, f"kline_{stamp}_raw.txt")
    csv_path = os.path.join(args.logdir, f"kline_{stamp}_decoded.csv")
    # line-buffered (buffering=1): elke regel wordt direct weggeschreven, zodat
    # er geen data verloren gaat als het proces hard wordt afgebroken.
    raw_f = open(raw_path, "w", buffering=1)
    csv_f = open(csv_path, "w", buffering=1)
    csv_f.write("time,len,kind,checksum,rpm,velocity,error_hex,temp_raw,temp_c,"
                "fault_code,fault_desc\n")

    print(f"{C.BOLD}Yamaha MT-03 K-line reader{C.RESET}")
    print(f"  Poort   : {port}")
    print(f"  Baud    : {args.baud}")
    print(f"  Ruwe log: {raw_path}")
    print(f"  CSV log : {csv_path}")
    print(f"  {C.DIM}spatie = pauze/hervat loggen, q = stoppen{C.RESET}\n")

    fs = FrameSync()
    paused = False
    stats = {"frames": 0, "data": 0, "bad": 0, "temp_open": 0}
    faults_seen = {}   # code -> aantal keer gezien
    t0 = time.monotonic()

    def flush(frame: bytes):
        if not frame:
            return
        stats["frames"] += 1
        ts = f"{time.monotonic() - t0:8.2f}"
        d = decode_frame(frame, temp_offset=args.temp_offset,
                         fault_encoding=args.fault_encoding)

        # ruwe log altijd
        if not paused:
            raw_f.write(d.hex() + "\n")

        if d.kind in ("data5", "data6"):
            stats["data"] += 1
            ok = d.checksum_ok
            if not ok:
                stats["bad"] += 1
            col = C.GREEN if ok else C.RED
            mark = "OK " if ok else "BAD"
            # Herkende Yamaha-foutcode krijgt voorrang in de weergave.
            if d.fault_code is not None:
                faults_seen[d.fault_code] = faults_seen.get(d.fault_code, 0) + 1
                errtxt = (f"  {C.RED}{C.BOLD}!! FOUT {d.fault_code}: "
                          f"{d.fault_desc}{C.RESET} "
                          f"{C.DIM}(0x{d.error:02x}, {d.fault_read}){C.RESET}")
            elif d.error == 0:
                errtxt = ""
            else:
                flags = error_flags(d.error)
                errtxt = f"  {C.YELLOW}status 0x{d.error:02x} [{' '.join(flags)}]{C.RESET}"
            # Tempbyte 0xFF = geen geldige koelvloeistoftemperatuur in dit frame.
            if d.temp_open:
                stats["temp_open"] += 1
                temptxt = f"{C.RED}{C.BOLD}temp 0xff(ongeldig){C.RESET}"
            else:
                temptxt = f"temp {d.temp_c:3d}"
            line = (
                f"{C.DIM}{ts}{C.RESET} "
                f"{col}[{mark}]{C.RESET} "
                f"RPM {C.BOLD}{d.rpm:5d}{C.RESET}  "
                f"km/h(ruw) {d.velocity:3d}  "
                f"{temptxt}  "
                f"err 0x{d.error:02x}{errtxt}"
            )
            if args.raw:
                line += f"   {C.DIM}{d.hex()}{C.RESET}"
            print(line)
            if not paused:
                fc = d.fault_code if d.fault_code is not None else ""
                fd = f"\"{d.fault_desc}\"" if d.fault_desc else ""
                csv_f.write(
                    f"{ts.strip()},{d.length},{d.kind},"
                    f"{'ok' if ok else 'bad'},{d.rpm},{d.velocity},"
                    f"0x{d.error:02x},{d.temp_raw},{d.temp_c},{fc},{fd}\n"
                )
        elif args.raw:
            # immo/idle/onbekend alleen tonen in raw-modus
            print(f"{C.DIM}{ts} [{d.kind}] {d.hex()}{C.RESET}")

    # SIGTERM netjes afvangen zodat de finally-blok (poort/bestanden sluiten)
    # ook loopt als de tool van buitenaf wordt gestopt.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        with RawKeyboard() as kb:
            while True:
                # lees wat er is (of blokkeer kort) en synchroniseer op checksum
                data = ser.read(ser.in_waiting or 1)
                if data:
                    for frame in fs.feed(data):
                        flush(frame)

                key = kb.getch()
                if key:
                    if key == " ":
                        paused = not paused
                        state = "GEPAUZEERD" if paused else "HERVAT"
                        print(f"{C.CYAN}-- loggen {state} --{C.RESET}")
                    elif key in ("q", "\x03"):  # q of Ctrl-C
                        break
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        raw_f.close()
        csv_f.close()
        dur = time.monotonic() - t0
        print(f"\n{C.BOLD}Gestopt.{C.RESET} "
              f"{stats['frames']} frames, {stats['data']} dataframes, "
              f"{stats['bad']} checksum-fouten, {dur:.0f}s.")
        if stats["temp_open"]:
            print(f"{C.RED}{C.BOLD}!! Temp-byte = 0xff (ONGELDIG) in "
                  f"{stats['temp_open']} frames -- geen geldige koelvloeistoftemp. "
                  f"Controleer sensor EN bedrading.{C.RESET}")
        if faults_seen:
            print(f"{C.RED}{C.BOLD}Herkende foutcodes tijdens deze sessie:{C.RESET}")
            for code in sorted(faults_seen):
                print(f"  {C.RED}FOUT {code}{C.RESET}: "
                      f"{FAULT_CODES.get(code, '?')}  "
                      f"{C.DIM}({faults_seen[code]}x){C.RESET}")
        else:
            print("Geen bekende foutcodes gezien.")
        print(f"Logs opgeslagen:\n  {raw_path}\n  {csv_path}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Live Yamaha K-line ECU reader (Ubuntu/Linux)."
    )
    p.add_argument("-p", "--port", help="seriële poort, bv. /dev/ttyUSB0")
    p.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD,
                   help=f"baudrate (default {DEFAULT_BAUD})")
    p.add_argument("--temp-offset", type=int, default=0,
                   help="offset op koelvloeistoftemp-byte (kalibratie)")
    p.add_argument("--fault-encoding", default="auto",
                   choices=["auto", "bcd", "decimal", "raw"],
                   help="hoe het foutcode-byte wordt gelezen (default auto)")
    p.add_argument("--logdir", default="log", help="map voor logbestanden")
    p.add_argument("--raw", action="store_true",
                   help="toon ook ruwe hex en immo/idle-frames")
    p.add_argument("--no-color", action="store_true", help="geen ANSI-kleuren")
    return p.parse_args(argv)


if __name__ == "__main__":
    run(parse_args())
