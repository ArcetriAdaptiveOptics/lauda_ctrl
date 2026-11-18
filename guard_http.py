# encoding: utf-8
"""
guard_http.py - read-only web viewer for the condensation_guard CSV logs.

Serves the guard_*.csv files in the log directory over HTTP (standard library
only, no extra dependencies):

    /                     page with plots (Plotly, loaded by the browser)
    /api/data?hours=24    columns of the last N hours as JSON (hours=0: all)
    /api/latest           last logged row as JSON
    /files/               list of the CSV logs
    /files/<name>.csv     download one CSV log

Started automatically by condensation_guard.py and condensation_guard_gui.py
(--http-port, default 3000, 0 disables), or stand-alone without the loop:

    python guard_http.py                   # http://<this pc>:3000/
    python guard_http.py --http-port 8080 --log-dir data
"""

__version__ = "0.2.0"

import argparse
import csv
import glob
import html
import json
import math
import os
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_HTTP_PORT = 3000
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MAX_POINTS = 20_000                # per request; longer ranges are decimated
NUM_KEYS = ["T1", "RH1", "T2", "T3", "Td", "surf_margin", "bump", "target",
            "sp_cmd", "sp_active", "pv"]
FILE_RE = re.compile(r"^guard_(\d{8}_\d{6})\.csv$")


# ---------------------------------------------------------------------------
# CSV access
# ---------------------------------------------------------------------------
def log_files(log_dir):
    """guard_*.csv files in log_dir, oldest first."""
    files = glob.glob(os.path.join(log_dir, "guard_*.csv"))
    return sorted(f for f in files if FILE_RE.match(os.path.basename(f)))


def _num(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v       # JSON has no NaN


def read_rows(log_dir, hours=24.0):
    """Columns {key: [...]} of the rows logged in the last `hours` (0 = all).

    A null row is inserted between files so plots do not join separate runs.
    """
    out = {k: [] for k in ["time", "mode", "lauda_stat"] + NUM_KEYS}
    t_lo = datetime.now() - timedelta(hours=hours) if hours > 0 else None
    for f in log_files(log_dir):
        if t_lo is not None and datetime.fromtimestamp(os.path.getmtime(f)) < t_lo:
            continue                           # file ended before the window
        try:
            with open(f, newline="") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            continue
        added = False
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if t_lo is not None and ts < t_lo:
                continue
            out["time"].append(r["time"])
            out["mode"].append(r.get("mode", ""))
            out["lauda_stat"].append(r.get("lauda_stat", ""))
            for k in NUM_KEYS:
                out[k].append(_num(r.get(k)))
            added = True
        if added:                              # gap marker between runs
            gap = datetime.fromisoformat(out["time"][-1]) + timedelta(seconds=1)
            out["time"].append(gap.isoformat(timespec="seconds"))
            out["mode"].append("")
            out["lauda_stat"].append("")
            for k in NUM_KEYS:
                out[k].append(None)
    return out


def decimate(cols, max_points=MAX_POINTS):
    """Keep every n-th row so that at most ~max_points remain."""
    n = len(cols["time"])
    if n <= max_points:
        return cols
    step = math.ceil(n / max_points)
    return {k: v[::step] for k, v in cols.items()}


def latest_row(log_dir):
    """Last row of the newest CSV, with the file's modification time."""
    files = log_files(log_dir)
    if not files:
        return None
    f = files[-1]
    try:
        with open(f, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    if not rows:
        return None
    r = rows[-1]
    row = {k: (_num(r.get(k)) if k in NUM_KEYS else r.get(k)) for k in r}
    row["file"] = os.path.basename(f)
    return row


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Condensation Guard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
 body{font-family:system-ui,sans-serif;margin:0 16px;background:#fafafa;color:#222}
 header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:10px 0}
 h1{font-size:1.2em;margin:0 12px 0 0}
 #mode{padding:3px 10px;border-radius:4px;color:#fff;font-weight:bold;background:#757575}
 #status{font-family:monospace;font-size:.9em}
 #age.stale{color:#c62828;font-weight:bold}
 .plot{width:100%;height:42vh;min-height:260px}
 .small{height:24vh;min-height:180px}
 footer{font-size:.85em;padding:8px 0 16px}
</style></head><body>
<header>
 <h1>Condensation Guard</h1>
 <span id="mode">-</span>
 <label>Finestra
  <select id="win">
   <option value="1">1 h</option><option value="4">4 h</option>
   <option value="12">12 h</option><option value="24" selected>24 h</option>
   <option value="168">7 giorni</option><option value="720">30 giorni</option>
   <option value="0">tutto</option>
  </select></label>
 <span id="status"></span> <span id="age"></span>
</header>
<div id="temp" class="plot"></div>
<div id="margin" class="plot small"></div>
<div id="rh" class="plot small"></div>
<footer><a href="files/">Scarica i file CSV</a> &middot; aggiornamento ogni 30 s</footer>
<script>
const COLORS = {FF:"#2e7d32", FB:"#ef6c00", HOLD:"#f9a825", FAILSAFE:"#c62828"};
const TEMP = [["T1","T ambiente","#d62728","solid"],["T2","T chassis","#9467bd","solid"],
  ["T3","T tubo","#8c564b","solid"],["Td","Dew point","#1f77b4","dash"],
  ["sp_active","Set point chiller","#000","solid"],["pv","Mandata chiller (PV)","#7f7f7f","dot"]];
const layout = (title, ytitle) => ({title:{text:title,font:{size:14}},
  margin:{l:50,r:10,t:30,b:30}, yaxis:{title:ytitle}, legend:{orientation:"h"},
  uirevision:"keep"});
const cfg = {responsive:true, displaylogo:false};

async function refresh() {
  const hours = document.getElementById("win").value;
  const d = await (await fetch("api/data?hours=" + hours)).json();
  Plotly.react("temp", TEMP.map(([k,n,c,s]) => ({x:d.time, y:d[k], name:n,
    mode:"lines", line:{color:c, dash:s, width:1.5}})), layout("Temperature","°C"), cfg);
  Plotly.react("margin", [{x:d.time, y:d.surf_margin, name:"min(Tsup) - Td",
    mode:"lines", line:{color:"#1f77b4"}}], layout("Margine superfici - dew point","°C"), cfg);
  Plotly.react("rh", [{x:d.time, y:d.RH1, name:"RH ambiente",
    mode:"lines", line:{color:"#17becf"}}], layout("Umidità ambiente","%"), cfg);

  const r = await (await fetch("api/latest")).json();
  if (!r) return;
  const f = (v, n=2) => v == null ? "-" : v.toFixed(n);
  const m = document.getElementById("mode");
  m.textContent = r.mode; m.style.background = COLORS[r.mode] || "#757575";
  document.getElementById("status").textContent =
    `Tamb ${f(r.T1)}  RH ${f(r.RH1,1)}  Td ${f(r.Td)}  m ${f(r.surf_margin)}  ` +
    `SP ${f(r.sp_active,1)}  PV ${f(r.pv)}  stat ${r.lauda_stat}`;
  const age = (Date.now() - new Date(r.time)) / 1000;
  const a = document.getElementById("age");
  a.textContent = `(${r.time}, ${Math.round(age)} s fa)`;
  a.className = age > 120 ? "stale" : "";
}
document.getElementById("win").onchange = refresh;
refresh(); setInterval(refresh, 30000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    log_dir = DEFAULT_LOG_DIR                  # set by make_server()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif path == "/api/data":
                q = parse_qs(url.query)
                hours = float(q.get("hours", ["24"])[0])
                self._json(decimate(read_rows(self.log_dir, hours)))
            elif path == "/api/latest":
                self._json(latest_row(self.log_dir))
            elif path in ("/files", "/files/"):
                self._file_list()
            elif path.startswith("/files/"):
                self._file(path[len("/files/"):])
            else:
                self._send(404, "text/plain", b"not found")
        except ValueError:
            self._send(400, "text/plain", b"bad request")

    def _send(self, code, ctype, body, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, "application/json", json.dumps(obj, allow_nan=False).encode())

    def _file_list(self):
        items = []
        for f in reversed(log_files(self.log_dir)):
            name = os.path.basename(f)
            mtime = datetime.fromtimestamp(os.path.getmtime(f)).isoformat(" ", "seconds")
            items.append(f'<li><a href="{html.escape(name)}">{html.escape(name)}</a> '
                         f'&middot; {os.path.getsize(f) / 1e6:.1f} MB &middot; fino a {mtime}</li>')
        body = ("<!doctype html><meta charset='utf-8'><title>CSV logs</title>"
                "<body style='font-family:system-ui,sans-serif'><h1>CSV logs</h1>"
                f"<p><a href='../'>&larr; grafici</a></p><ul>{''.join(items)}</ul>")
        self._send(200, "text/html; charset=utf-8", body.encode())

    def _file(self, name):
        if not FILE_RE.match(name):            # no path traversal: plain log names only
            self._send(404, "text/plain", b"not found")
            return
        f = os.path.join(self.log_dir, name)
        if not os.path.isfile(f):
            self._send(404, "text/plain", b"not found")
            return
        with open(f, "rb") as fh:
            body = fh.read()
        self._send(200, "text/csv", body,
                   [("Content-Disposition", f'attachment; filename="{name}"')])

    def log_message(self, fmt, *args):         # keep the console / GUI log quiet
        pass


def make_server(port=DEFAULT_HTTP_PORT, log_dir=DEFAULT_LOG_DIR, host=""):
    handler = type("GuardHandler", (Handler,), {"log_dir": log_dir})
    return ThreadingHTTPServer((host, port), handler)


def start_in_background(port=DEFAULT_HTTP_PORT, log_dir=DEFAULT_LOG_DIR, log=print):
    """Start the server in a daemon thread. A failure (e.g. port in use) is only
    logged: the web viewer must never stop the control loop."""
    if not port:
        return None
    try:
        srv = make_server(port, log_dir)
    except OSError as e:
        log(f"[http] cannot listen on port {port}: {e}")
        return None
    threading.Thread(target=srv.serve_forever, name="guard-http", daemon=True).start()
    log(f"[http] serving {log_dir} on http://localhost:{port}/")
    return srv


def main():
    p = argparse.ArgumentParser(description="Web viewer for the condensation_guard CSV logs")
    p.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    a = p.parse_args()
    srv = make_server(a.http_port, a.log_dir)
    print(f"[http] serving {a.log_dir} on http://localhost:{a.http_port}/  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
