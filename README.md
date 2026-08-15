# Yamaha K-line reader (Ubuntu / Python 3)

Live uitlezen van de ECU van een pre-2015 Yamaha (Euro 3) — o.a. de **MT-03 660 (2010)** —
via de 3-pins diagnosestekker en een **KKL 409.1 USB-kabel (FTDI FT232RL)**.
Geschreven voor **Ubuntu Desktop / Linux**, zonder Arduino en zonder Windows-afhankelijkheden.

Gebaseerd op het reverse-engineeringwerk van
[terrafirma2021/Yamaha-K-line-Tool](https://github.com/terrafirma2021/Yamaha-K-line-Tool)
en [/Yamaha-K-line-sniffer](https://github.com/terrafirma2021/Yamaha-K-line-sniffer)
(ESP32/Windows), herschreven naar pure Python 3 met **checksum-validatie, decodering en
vertaling van Yamaha-foutcodes**.

## Wat het doet

- Leest **passief** mee op de K-line (het instrumentenpaneel pollt de ECU, wij luisteren mee — geen init-sequentie nodig).
- Herkent frames via de inter-frame gap en **valideert de checksum**.
- Decodeert live: **toerental, snelheid-byte, error/status-byte, koelvloeistoftemperatuur**.
- **Vertaalt Yamaha-foutcodes** naar leesbare tekst zodra ze optreden (zie hieronder).
- Logt alles naar `log/` met tijdstempel: ruwe hex + een `decoded.csv` voor analyse in Excel/Python.

## Het protocol (gevalideerd tegen echte logs)

```
6-byte frame:  01  RPM  VEL  ERR  TEMP  CHK
5-byte frame:      RPM  VEL  ERR  TEMP  CHK      (zelfde payload, zonder de 0x01 request-byte)

CHK  = (RPM + VEL + ERR + TEMP) & 0xFF          (de 0x01 telt NIET mee)
RPM  = byte * 50                                 (toeren/min)
VEL  = snelheid-byte (ruw; op de standaard = 0)
ERR  = status/error-byte (bitmasker; 0x00 = geen actieve vlag)
TEMP = koelvloeistoftemp-byte (ruw — zie 'Temperatuur kalibreren')
```

Op een volledige capture valideert **~99,4%** van de dataframes correct (op de originele
log 4630 van 4656); de rest zijn immobilizer-handshake bytes, geen dataframes.
`examples/ECU_Log_sample.txt` is een compact, geannoteerd fragment van zo'n capture met
elk protocolblok, zodat `decode_log.py` meteen iets heeft om op te draaien.

## Foutcodes

De tool kent de Yamaha zelfdiagnose-codes (12 t/m 50) en toont bij een herkende code
direct de betekenis, bv:

```
   34.7 [OK ] RPM  1250  km/h(ruw)  0  temp 78  err 0x21  !! FOUT 21: Koelvloeistoftemperatuursensor open of kortsluiting (0x21, bcd)
```

Bij het stoppen (en in `decode_log.py`) volgt een samenvatting van alle herkende codes.

**Belangrijk / eerlijk:** in een normale runlog is het error-byte meestal `0x00` (draait) of
`0x80` (koude idle) — dat zijn statuswaarden, geen foutnummers. Echte foutcodes verschijnen
pas als er een storing actief is (vaak in diagnosemodus). De exacte on-wire codering van de
code is voor deze stream niet officieel bevestigd, daarom is die instelbaar met
`--fault-encoding`:

| Waarde | Betekenis |
|---|---|
| `auto` (default) | probeer eerst BCD, dan decimaal |
| `bcd` | lees de twee hex-cijfers als decimaal (`0x21` → code 21) |
| `decimal` | lees de bytewaarde als decimaal (`0x15` → code 21) |
| `raw` | geen foutcode-vertaling, alleen ruwe status |

De volledige codelijst staat in `kline_protocol.py` (`FAULT_CODES`).

## Installatie (Ubuntu)

```bash
# 1. Python-afhankelijkheid
sudo apt update && sudo apt install -y python3-pip
pip3 install -r requirements.txt        # of: sudo apt install python3-serial

# 2. Toegang tot de seriële poort zonder sudo
sudo usermod -aG dialout $USER          # daarna uitloggen/inloggen

# 3. (optioneel) vaste naam /dev/kkl voor de kabel
sudo cp 99-ftdi-kkl.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Controleer of de kabel gezien wordt:

```bash
ls -l /dev/ttyUSB*        # verwacht /dev/ttyUSB0
dmesg | grep -i ftdi      # bevestigt de FT232RL-driver
```

## Gebruik

```bash
# contact aan (ECU/dashboard gevoed), kabel op de 3-pins stekker, dan:
python3 mt03_kline.py                      # auto-detecteert /dev/ttyUSB*
python3 mt03_kline.py -p /dev/ttyUSB0      # expliciete poort
python3 mt03_kline.py --raw                # toon ook ruwe hex + immo/idle-frames
python3 mt03_kline.py --temp-offset -30    # temp-kalibratie toepassen
python3 mt03_kline.py --fault-encoding bcd # forceer een foutcode-lezing
```

Toetsen tijdens draaien: **spatie** = pauzeer/hervat loggen, **q** of **Ctrl-C** = stoppen.

### Opgeslagen logs her-analyseren

```bash
python3 decode_log.py examples/ECU_Log_sample.txt          # samenvatting + foutcodes
python3 decode_log.py log/kline_..._raw.txt --csv uit.csv  # naar CSV
```

## Diagnose: "loopt kort en slaat af na starten"

Deze tool is gebouwd om precies dat probleem te helpen vinden. Let bij het meelezen op:

- **Foutcodes** — een herkende code (bv. `FOUT 21` koelvloeistofsensor, `FOUT 30` kantelhoek,
  `FOUT 42` snelheidssensor) wijst direct een defecte sensor/circuit aan. De samenvatting bij
  het stoppen laat zien welke codes tijdens de sessie optraden.
- **Error/status-byte (`err`)** — in de voorbeeldlog stond `0x80` (bit7) actief tijdens de
  koude idle en `0x00` tijdens normaal draaien. Kijk of er vlak vóór het afslaan iets omklapt.
- **RPM-verloop** — stort het toerental in (naar 0) op het moment van afslaan? Vergelijk het
  laatste dataframe vóór stilstand.
- **Immobilizer** — de logs bevatten een "Immo Block" handshake bij contact-aan. Als de
  motor afslaat door een immobilizer-blokkade zie je dat rond die handshake.
- **Temperatuur** — na kleppen stellen kan een verkeerd gemonteerde/aangesloten
  temperatuursensor de ECU verkeerde waarden geven; controleer of `temp` plausibel meebeweegt.

Draai de tool tijdens een start-tot-afslaan sessie en bewaar de `decoded.csv` — daarmee kun
je het exacte frame op het moment van afslaan terugzoeken.

## Non-standaard baudrate (16064)

16064 baud is niet-standaard. `pyserial` zet dit op Linux via de FTDI-driver. Werkt dat op
jouw kernel niet, controleer dan de daadwerkelijke instelling:

```bash
stty -F /dev/ttyUSB0
```

Zie je een afwijkende snelheid, dan kun je de FTDI-latency verlagen en de baud forceren; in de
praktijk werkt `pyserial` met `baudrate=16064` op recente Ubuntu-kernels direct.

## Bestanden

| Bestand | Doel |
|---|---|
| `mt03_kline.py` | Live reader + decoder + logger + foutcodevertaling |
| `decode_log.py` | Offline her-analyse van opgeslagen/vrije-vorm hex-logs |
| `kline_protocol.py` | Protocol-decoder + `FAULT_CODES` tabel — herbruikbaar |
| `99-ftdi-kkl.rules` | Udev-regel voor vaste poortnaam + rechten |
| `examples/ECU_Log_sample.txt` | Geannoteerd voorbeeldlog (fragment) |

## Disclaimer

Passief meelezen op de K-line is niet-invasief, maar werk zorgvuldig rond een draaiende motor.
De veld- en bit-interpretaties en de foutcode-codering zijn afgeleid van reverse-engineering
en niet officieel door Yamaha bevestigd.
