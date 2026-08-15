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
from typing import List, Optional

# Koelvloeistoftemperatuur: de exacte schaal/offset van de MT-03 is niet
# gedocumenteerd in de originele log. De ruwe byte wordt altijd getoond.
# Zet dit op de offset die je vindt zodra je 'm kalibreert tegen een bekende
# warme temperatuur (bv. via de dashboard-temperatuurweergave).
DEFAULT_TEMP_OFFSET = 0


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

    def hex(self) -> str:
        return " ".join(f"{b:02x}" for b in self.raw)


def _checksum(payload: bytes) -> int:
    """CHK over de eerste 4 payload-bytes (RPM, VEL, ERR, TEMP)."""
    return sum(payload[:4]) & 0xFF


def decode_frame(frame: bytes, temp_offset: int = DEFAULT_TEMP_OFFSET) -> Decoded:
    """Decodeer een enkel frame (bytes)."""
    n = len(frame)

    # 6-byte data-frame: 01 RPM VEL ERR TEMP CHK
    if n == 6 and frame[0] == 0x01:
        payload = frame[1:]           # RPM VEL ERR TEMP CHK
        ok = _checksum(payload) == payload[4]
        temp_raw = payload[3]
        return Decoded(
            raw=frame, length=n, kind="data6", checksum_ok=ok,
            rpm=payload[0] * 50, velocity=payload[1], error=payload[2],
            temp_raw=temp_raw, temp_c=temp_raw + temp_offset,
        )

    # 5-byte data-frame: RPM VEL ERR TEMP CHK
    if n == 5:
        ok = _checksum(frame) == frame[4]
        temp_raw = frame[3]
        return Decoded(
            raw=frame, length=n, kind="data5", checksum_ok=ok,
            rpm=frame[0] * 50, velocity=frame[1], error=frame[2],
            temp_raw=temp_raw, temp_c=temp_raw + temp_offset,
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
