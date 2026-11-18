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
python condensation_guard_gui.py --autostart  # GUI with live plots (+ web viewer on :3000)
python crea_icona_desktop.py                  # create the desktop icon (Windows)
python lauda_scan.py --port COM3              # scan serial settings on a local COM port
```

Every loop cycle is logged to `data/guard_YYYYMMDD_HHMMSS.csv`; the GUI reloads the
last 24 h of logs at startup.

## Web viewer

Both the CLI and the GUI also serve the CSV logs read-only over HTTP on
`--http-port` (default **3000**, `0` disables it): open `http://<this pc>:3000/`
for plots of the last hours/days/everything, refreshed every 30 s, and
`/files/` to download the CSV files. The plots load Plotly from `cdn.plot.ly`,
so the browser needs Internet access. Only the standard library is used
server-side.

```
python guard_http.py --http-port 3000   # viewer only, without running the loop
```

| URL | Content |
|---|---|
| `/` | plots + current status |
| `/api/data?hours=24` | columns of the last N hours as JSON (`hours=0`: all, decimated to 20000 points) |
| `/api/latest` | last logged row |
| `/files/` | list / download of the CSV logs |

There is no authentication: anyone who can reach the port can read the logs
(nothing can be changed from the web). To reach it from other PCs, Windows
Firewall must allow inbound TCP on that port.

Manuals used: LAUDA Q4DA-E_13-039 (Microcool, V1R26, ch. 6.8 RS 232) and the Moxa
NPort manual — not included in the repository.
