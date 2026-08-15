# Yamaha K-line reader (Ubuntu / Python 3)

Live uitlezen van de ECU van een pre-2015 Yamaha (Euro 3) — o.a. de **MT-03 660 (2010)** —
via de 3-pins diagnosestekker en een **KKL 409.1 USB-kabel (FTDI FT232RL)**.
Geschreven voor **Ubuntu Desktop / Linux**, zonder Arduino en zonder Windows-afhankelijkheden.

Gebaseerd op het reverse-engineeringwerk van
[terrafirma2021/Yamaha-K-line-Tool](https://github.com/terrafirma2021/Yamaha-K-line-Tool)
en [/Yamaha-K-line-sniffer](https://github.com/terrafirma2021/Yamaha-K-line-sniffer)
(ESP32/Windows), herschreven naar pure Python 3 met **checksum-validatie en decodering**.

## Wat het doet

- Leest **passief** mee op de K-line (het instrumentenpaneel pollt de ECU, wij luisteren mee — geen init-sequentie nodig).
- Herkent frames via de inter-frame gap en **valideert de checksum**.
- Decodeert live: **toerental, snelheid-byte, error/status-byte, koelvloeistoftemperatuur**.
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

Op de meegeleverde voorbeeldlog (`examples/ECU_Log_sample.txt`) valideert **4630 van de
4656** dataframes correct; de rest zijn immobilizer-handshake bytes (geen dataframes).

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
```

Toetsen tijdens draaien: **spatie** = pauzeer/hervat loggen, **q** of **Ctrl-C** = stoppen.

Voorbeeld-uitvoer:

```
   12.34 [OK ] RPM  2050  km/h(ruw)   0  temp  46  err 0x80  ERR bit7 (0x80)
   12.37 [OK ] RPM  2450  km/h(ruw)   0  temp  46  err 0x00
```

### Opgeslagen logs her-analyseren

```bash
python3 decode_log.py examples/ECU_Log_sample.txt          # samenvatting + histogram
python3 decode_log.py log/kline_..._raw.txt --csv uit.csv  # naar CSV
```

## Diagnose: "loopt kort en slaat af na starten"

Deze tool is gebouwd om precies dat probleem te helpen vinden. Let bij het meelezen op:

- **Error/status-byte (`err`)** — in de voorbeeldlog stond `0x80` (bit7) actief tijdens de
  koude idle en `0x00` tijdens normaal draaien. Kijk of er vlak vóór het afslaan een bit
  omklapt. De exacte bit-betekenis is niet officieel gedocumenteerd; het histogram in
  `decode_log.py` helpt patronen herkennen.
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
| `mt03_kline.py` | Live reader + decoder + logger |
| `decode_log.py` | Offline her-analyse van opgeslagen/vrije-vorm hex-logs |
| `kline_protocol.py` | Protocol-decoder (frames, checksum, velden) — herbruikbaar |
| `99-ftdi-kkl.rules` | Udev-regel voor vaste poortnaam + rechten |
| `examples/ECU_Log_sample.txt` | Voorbeeldlog met protocoluitleg |

## Disclaimer

Passief meelezen op de K-line is niet-invasief, maar werk zorgvuldig rond een draaiende motor.
De veld- en bit-interpretaties zijn afgeleid van reverse-engineering en niet officieel door
Yamaha bevestigd.
