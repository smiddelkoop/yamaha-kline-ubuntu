"""
Yamaha K-line protocol decoder (pre-2015 Euro3 bikes, o.a. MT-03 660 2010).

Frame-formaat zoals afgeleid en gevalideerd tegen echte ECU-logs:

  6-byte frame:  01  RPM  VEL  ERR  TEMP  CHK
  5-byte frame:      RPM  VEL  ERR  TEMP  CHK   (zelfde payload, zonder de
                                                leidende 0x01 request-byte)

  CHK = (RPM + VEL + ERR + TEMP) & 0xFF   (de 0x01 telt NIET mee)

Velden:
  RPM   = byte * 50          (toeren per minuut)
  VEL   = snelheid-byte      (ruw; volgens de originele log-notitie moeten er
                              8 opgeteld worden voor km/h -> hier ruw getoond)
  ERR   = status/error-byte  (bitmasker; 0x00 = geen actieve vlag)
  TEMP  = koelvloeistoftemp  (ruw; zie TEMP_OFFSET hieronder)

Deze module bevat geen serial-code, zodat je 'm ook los kunt gebruiken om
opgeslagen logs te her-analyseren.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

# Koelvloeistoftemperatuur: de exacte schaal/offset van de MT-03 is niet
# gedocumenteerd in de originele log. De ruwe byte wordt altijd getoond.
# Zet dit op de offset die je vindt zodra je 'm kalibreert tegen een bekende
# warme temperatuur (bv. via de dashboard-temperatuurweergave).
DEFAULT_TEMP_OFFSET = 0

# Tempbyte 0xFF = geen geldige koelvloeistoftemperatuur in dit frame
# (ongeldig/placeholder). Kan wijzen op een sensor-/bedradingsprobleem, maar
# is NIET automatisch bewijs van een kapotte sensor -- interpreteer voorzichtig.
TEMP_OPEN = 0xFF

# Immobilizer-handshake en andere niet-telemetrie frames kunnen toevallig een
# geldige checksum hebben en dan als spookdata verschijnen (valse snelheid/temp/
# foutcode). We herkennen ze en tellen ze niet als motordata:
#   - alle vier de payload-bytes gelijk (bv. 47 47 47 47, 45 45 45 45), of
#   - een onmogelijk hoog toerental (RPM-byte boven de redline).
REDLINE_RPM = 9000

# ---------------------------------------------------------------------------
# Yamaha ECU-foutcodes (de 2-cijferige codes die de zelfdiagnose toont).
# Bron: door de gebruiker aangeleverde "Core Yamaha ECU Fault Codes".
# ---------------------------------------------------------------------------
FAULT_CODES = {
    12: "Krukas-positiesensor (CKP) fout",
    13: "Inlaatdruksensor (MAP) open of kortsluiting",
    14: "Inlaatdruksensor (MAP) signaalfout / vacuumlek",
    15: "Smoorklep-positiesensor (TPS) circuitfout",
    16: "ECU interne hardwarefout",
    17: "EXUP-servomotor circuitfout",
    18: "EXUP-servomotor zit vast",
    19: "Zijstandaardschakelaar open circuit / losgekoppeld",
    21: "Koelvloeistoftemperatuursensor open of kortsluiting",
    22: "Inlaatluchttemperatuursensor (IAT) circuitfout",
    23: "Atmosferische/barometrische druksensor fout",
    24: "O2 (lambda) sensor ontbreekt of abnormaal signaal",
    30: "Kantelhoeksensor geactiveerd (voertuig gevallen/gekanteld)",
    33: "Bobine cilinder #1 primair circuit fout",
    34: "Bobine cilinder #2 primair circuit fout",
    37: "Stationairtoerenregelaar (ISC) klep defect",
    39: "Brandstofinjector open of kortsluiting",
    41: "Kantelhoeksensor interne hardwarefout",
    42: "Snelheidssensor (VSS) of ABS-signaalfout",
    43: "Brandstofpomp/injector voedingslijn fout",
    44: "EEPROM-fout (CO-afstelwaarde lezen/schrijven mislukt)",
    46: "Voedingsfout (abnormale spanning, laad- of accufout)",
    50: "Defecte ECU intern geheugen",
}

# Foutcode-vertaling uit het error-byte.
#
# BELANGRIJK: in de tot nu toe opgenomen streams is het error-byte een
# STATUS-byte (bv. 0x00 gezond, 0x40/0x80 bij problemen) en GEEN Yamaha
# storingscodekanaal. Elke "foutcode" die je daaruit afleidt is daarom vals
# (bv. immo-handshake bytes die toevallig als 0x43 -> code 43 uitkomen).
# Daarom staat de vertaling standaard UIT ("raw"). Zet 'm alleen aan als je een
# echte diagnosemodus-stream met stored codes uitleest.
#   "raw"     -> geen foutcode-vertaling (default)
#   "bcd"     -> lees de twee hex-cijfers als decimaal  (0x42 -> code 42)
#   "decimal" -> lees de bytewaarde als decimaal         (0x2a -> code 42)
#   "auto"    -> probeer eerst bcd, dan decimal
DEFAULT_FAULT_ENCODING = "raw"


def decode_fault(error_byte: int,
                 encoding: str = DEFAULT_FAULT_ENCODING
                 ) -> Optional[Tuple[int, str, str]]:
    """
    Vertaal een byte naar een bekende Yamaha-foutcode.

    Retourneert (code, omschrijving, gebruikte_lezing) of None als er geen
    bekende foutcode uit volgt. 0x00 wordt nooit als fout gezien.
    """
    if encoding == "raw" or error_byte == 0:
        return None

    def _bcd(b):
        s = f"{b:02x}"
        if all(c in "0123456789" for c in s):
            code = int(s)
            if code in FAULT_CODES:
                return (code, FAULT_CODES[code], "bcd")
        return None

    def _dec(b):
        if b in FAULT_CODES:
            return (b, FAULT_CODES[b], "decimal")
        return None

    if encoding == "bcd":
        return _bcd(error_byte)
    if encoding == "decimal":
        return _dec(error_byte)
    # auto
    return _bcd(error_byte) or _dec(error_byte)


@dataclass
class Decoded:
    raw: bytes
    length: int
    kind: str                 # 'data6', 'data5', 'immo', 'idle', 'unknown'
    checksum_ok: Optional[bool]
    rpm: Optional[int] = None
    velocity: Optional[int] = None
    error: Optional[int] = None
    temp_raw: Optional[int] = None
    temp_c: Optional[int] = None
    fault_code: Optional[int] = None      # herkende Yamaha-foutcode (bv. 42)
    fault_desc: Optional[str] = None      # leesbare omschrijving
    fault_read: Optional[str] = None      # welke lezing gaf de match (bcd/decimal)
    temp_open: bool = False               # tempbyte == 0xFF (geen geldige temp)

    def hex(self) -> str:
        return " ".join(f"{b:02x}" for b in self.raw)


def _checksum(payload: bytes) -> int:
    """CHK over de eerste 4 payload-bytes (RPM, VEL, ERR, TEMP)."""
    return sum(payload[:4]) & 0xFF


def _looks_like_noise(payload: bytes) -> bool:
    """Immobilizer-handshake / niet-telemetrie frame? (zie REDLINE_RPM)."""
    p = payload[:4]
    if p[0] == p[1] == p[2] == p[3]:
        return True
    if p[0] * 50 > REDLINE_RPM:
        return True
    return False


def decode_frame(frame: bytes,
                 temp_offset: int = DEFAULT_TEMP_OFFSET,
                 fault_encoding: str = DEFAULT_FAULT_ENCODING) -> Decoded:
    """Decodeer een enkel frame (bytes)."""
    n = len(frame)

    # 6-byte data-frame: 01 RPM VEL ERR TEMP CHK
    if n == 6 and frame[0] == 0x01:
        payload = frame[1:]           # RPM VEL ERR TEMP CHK
        if _looks_like_noise(payload):
            return Decoded(raw=frame, length=n, kind="immo", checksum_ok=True)
        ok = _checksum(payload) == payload[4]
        temp_raw = payload[3]
        error = payload[2]
        fault = decode_fault(error, fault_encoding)
        return Decoded(
            raw=frame, length=n, kind="data6", checksum_ok=ok,
            rpm=payload[0] * 50, velocity=payload[1], error=error,
            temp_raw=temp_raw, temp_c=temp_raw + temp_offset,
            fault_code=fault[0] if fault else None,
            fault_desc=fault[1] if fault else None,
            fault_read=fault[2] if fault else None,
            temp_open=(temp_raw == TEMP_OPEN),
        )

    # 5-byte data-frame: RPM VEL ERR TEMP CHK
    if n == 5:
        if _looks_like_noise(frame):
            return Decoded(raw=frame, length=n, kind="immo", checksum_ok=True)
        ok = _checksum(frame) == frame[4]
        temp_raw = frame[3]
        error = frame[2]
        fault = decode_fault(error, fault_encoding)
        return Decoded(
            raw=frame, length=n, kind="data5", checksum_ok=ok,
            rpm=frame[0] * 50, velocity=frame[1], error=error,
            temp_raw=temp_raw, temp_c=temp_raw + temp_offset,
            fault_code=fault[0] if fault else None,
            fault_desc=fault[1] if fault else None,
            fault_read=fault[2] if fault else None,
            temp_open=(temp_raw == TEMP_OPEN),
        )

    # Immobilizer-handshake / idle-blokken hebben afwijkende lengtes/inhoud.
    if all(b == 0x00 for b in frame):
        kind = "idle"
    else:
        kind = "unknown"
    return Decoded(raw=frame, length=n, kind=kind, checksum_ok=None)


def error_flags(error: int) -> List[str]:
    """
    Ontleed het error/status-byte in losse bits.

    De exacte bit-betekenis is (nog) niet officieel gedocumenteerd voor de
    MT-03. Uit de logs valt op dat bit7 (0x80) actief was tijdens een run met
    problemen en 0x00 was tijdens normaal draaien. Deze functie toont daarom
    welke bits gezet zijn, zodat je patronen kunt herkennen.
    """
    if error == 0:
        return ["geen vlaggen"]
    flags = []
    for bit in range(8):
        if error & (1 << bit):
            flags.append(f"bit{bit} (0x{1 << bit:02x})")
    return flags


class FrameSync:
    """
    Synchroniseer een byte-stream op frame-grenzen via de CHECKSUM, niet op
    tijd-gaps. Dit lost de misalignment op die ontstaat als de K-line-stream
    geen schone inter-frame gaps heeft (dan las de oude gap-methode frames
    verkeerd uit -> allemaal 'checksum bad').

    Herkent 6-byte frames (01 RPM VEL ERR TEMP CHK) en 5-byte frames
    (RPM VEL ERR TEMP CHK). Bij twijfel schuift hij 1 byte op tot een frame
    weer valideert; de stream herstelt zichzelf zo binnen enkele bytes.

    Gebruik (streaming):
        fs = FrameSync()
        for frame in fs.feed(chunk_bytes):
            ...
    of in een keer:
        frames = FrameSync().feed(hele_stream)
    """

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data) -> list:
        self.buf.extend(data)
        out = []
        b = self.buf
        while len(b) >= 5:
            n = len(b)
            if b[0] == 0x01 and n >= 6 and (sum(b[1:5]) & 0xFF) == b[5]:
                out.append(bytes(b[:6]))
                del b[:6]
            elif (sum(b[:4]) & 0xFF) == b[4]:
                out.append(bytes(b[:5]))
                del b[:5]
            elif b[0] == 0x01 and n < 6:
                break                    # wacht op meer bytes om 6-byte te testen
            else:
                del b[:1]                # resync: schuif 1 byte op
        return out


def frames_from_stream(data: bytes) -> list:
    """Gemaksfunctie: haal alle geldige frames uit een complete byte-stream."""
    return FrameSync().feed(data)
