# lauda_ctrl

Anti-condensation closed loop for a LAUDA Microcool MC 1200 chiller, driven by a
[Thor](https://github.com/ArcetriAdaptiveOptics/Thor) temperature/humidity sensor.

The chiller set point follows the ambient dew point (feedforward, `SP = Td + margin`)
with an integral correction on the coldest monitored surface (feedback), rate-limited
and clamped to the device limits. On loss of Thor data the set point goes to a
fail-safe value.

## Hardware

| Item | Address / setting |
|---|---|
| Thor | `192.168.30.134` (port 1 ambient T/RH, port 2 chassis, port 3 inlet tube) |
| Moxa NPort 5110A | `192.168.30.103`, TCP Server, data port **4660**, serial 9600 8N1, no flow control |
| Chiller RS 232 | 9600 8N1, **straight 1:1 cable** (chiller TxD on pin 2), replies end with CRLF |

The port can be either `socket://192.168.30.103:4660` (Moxa, default) or a local `COMx`.

## Usage

```
pip install -r requirements.txt
python condensation_guard.py --check          # test Thor + chiller, read only
python condensation_guard.py --dry-run        # run the loop without writing the chiller
python condensation_guard_gui.py --autostart  # GUI with live plots
python crea_icona_desktop.py                  # create the desktop icon (Windows)
python lauda_scan.py --port COM3              # scan serial settings on a local COM port
```

Every loop cycle is logged to `data/guard_YYYYMMDD_HHMMSS.csv`; the GUI reloads the
last 24 h of logs at startup.

Manuals used: LAUDA Q4DA-E_13-039 (Microcool, V1R26, ch. 6.8 RS 232) and the Moxa
NPort manual — not included in the repository.
