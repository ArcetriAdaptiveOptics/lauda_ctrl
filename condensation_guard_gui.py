# encoding: utf-8
"""
condensation_guard_gui.py - GUI for condensation_guard.py

Starts/stops the anti-condensation loop in a background thread and plots
live: ambient T, chassis T, tube T, dew point, chiller set point and outflow
(top), ambient RH (bottom).

    pythonw condensation_guard_gui.py                   # open, press Start
    pythonw condensation_guard_gui.py --autostart       # start immediately
    pythonw condensation_guard_gui.py --dry-run --autostart
"""

__author__ = "Marco Bonaglia"
__version__ = "0.2.0"

import argparse
import csv
import glob
import math
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

import condensation_guard as cg

MAX_POINTS = 100_000          # ~11 days at 10 s
WINDOWS = {"15 min": 15, "1 h": 60, "4 h": 240, "12 h": 720, "24 h": 1440, "tutto": None}
MODE_COLORS = {"FF": "#2e7d32", "FB": "#ef6c00", "HOLD": "#f9a825",
               "FAILSAFE": "#c62828", "STOP": "#757575"}

# (key, label, color, style)
TEMP_TRACES = [
    ("T1", "T ambiente", "tab:red", "-"),
    ("T2", "T chassis", "tab:purple", "-"),
    ("T3", "T tubo", "tab:brown", "-"),
    ("Td", "Dew point", "tab:blue", "--"),
    ("sp_active", "Set point chiller", "black", "-"),
    ("pv", "Mandata chiller (PV)", "tab:gray", ":"),
]


def load_history(log_dir, hours=24.0, keys=()):
    """Read guard_*.csv files from log_dir touched in the last `hours`.

    Returns {key: [...]} with "ts" as datetimes; a NaN row is inserted
    between files so the plot does not join separate sessions.
    """
    out = {k: [] for k in keys}
    if not os.path.isdir(log_dir):
        return out
    t_lo = datetime.now() - timedelta(hours=hours)
    files = sorted(glob.glob(os.path.join(log_dir, "guard_*.csv")))
    files = [f for f in files if datetime.fromtimestamp(os.path.getmtime(f)) >= t_lo]
    for f in files:
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
            if ts < t_lo:
                continue
            out["ts"].append(ts)
            for k in keys:
                if k == "ts":
                    continue
                try:
                    out[k].append(float(r.get(k, "nan")))
                except (TypeError, ValueError):
                    out[k].append(math.nan)
            added = True
        if added:                                  # gap marker between sessions
            out["ts"].append(out["ts"][-1] + timedelta(seconds=1))
            for k in keys:
                if k != "ts":
                    out[k].append(math.nan)
    return out


class GuardGUI(tk.Tk):
    def __init__(self, args):
        super().__init__()
        self.title("Condensation Guard - LAUDA Microcool + Thor")
        self.geometry("1200x820")
        self.minsize(900, 600)

        self.args = args
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.closing = False
        self.data = {k: [] for k in ["ts", "RH1"] + [t[0] for t in TEMP_TRACES]}
        self.dirty = False

        self._build_controls()
        self._build_readouts()
        self._build_plot()
        self._build_log()
        self._set_mode("STOP")

        # show what the data logger saved before this session (last 24 h)
        hist = load_history(args.log_dir, 24.0, list(self.data))
        if hist.get("ts"):
            self.data = hist
            self._log(f"Storico caricato da {args.log_dir}: {len(hist['ts'])} punti (ultime 24 h)")
            self.win_var.set("24 h")
            self.dirty = True

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(300, self._poll)
        if args.autostart:
            self.after(500, self.start)

    # ------------------------------------------------------------------ UI
    def _build_controls(self):
        f = ttk.Frame(self, padding=(8, 6))
        f.pack(side=tk.TOP, fill=tk.X)
        a = self.args
        self.vars = {}
        fields = [("Porta", "port", a.port, 28), ("Thor IP", "thor_ip", a.thor_ip, 15),
                  ("Periodo [s]", "interval", a.interval, 5),
                  ("Margine [C]", "margin", a.margin, 5),
                  ("Marg. superf. [C]", "surf_margin", a.surf_margin, 5),
                  ("SP min", "sp_min", a.sp_min, 5), ("SP max", "sp_max", a.sp_max, 5),
                  ("SP failsafe", "failsafe_sp", a.failsafe_sp, 5)]
        self.entries = []
        for i, (lab, key, val, w) in enumerate(fields):
            ttk.Label(f, text=lab).grid(row=0, column=i, sticky="w", padx=3)
            v = tk.StringVar(value=str(val))
            e = ttk.Entry(f, textvariable=v, width=w)
            e.grid(row=1, column=i, sticky="w", padx=3)
            self.vars[key] = v
            self.entries.append(e)
        n = len(fields)
        self.dry_var = tk.BooleanVar(value=a.dry_run)
        self.dry_chk = ttk.Checkbutton(f, text="Dry run (non scrive)", variable=self.dry_var)
        self.dry_chk.grid(row=1, column=n, padx=8)
        self.btn = tk.Button(f, text="▶  START", width=12, bg="#2e7d32", fg="white",
                             font=("Segoe UI", 11, "bold"), command=self.toggle)
        self.btn.grid(row=0, column=n + 1, rowspan=2, padx=8)
        self.mode_lbl = tk.Label(f, text="", width=12, font=("Segoe UI", 12, "bold"),
                                 fg="white", relief=tk.GROOVE)
        self.mode_lbl.grid(row=0, column=n + 2, rowspan=2, padx=4)

    def _build_readouts(self):
        f = ttk.Frame(self, padding=(8, 2))
        f.pack(side=tk.TOP, fill=tk.X)
        self.readouts = {}
        items = [("T1", "T amb", "°C"), ("RH1", "RH", "%"), ("Td", "Dew point", "°C"),
                 ("T2", "T chassis", "°C"), ("T3", "T tubo", "°C"),
                 ("surf_margin", "Margine superf.", "°C"), ("sp_active", "SP chiller", "°C"),
                 ("pv", "PV chiller", "°C"), ("bump", "Bump FB", "°C")]
        for i, (key, lab, unit) in enumerate(items):
            box = ttk.Frame(f, relief=tk.GROOVE, padding=(6, 2))
            box.grid(row=0, column=i, padx=3, sticky="nsew")
            f.columnconfigure(i, weight=1)
            ttk.Label(box, text=lab, font=("Segoe UI", 9)).pack()
            v = tk.StringVar(value="--")
            ttk.Label(box, textvariable=v, font=("Segoe UI", 15, "bold")).pack()
            self.readouts[key] = (v, unit)
        self.stat_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.stat_var, padding=(10, 0)).pack(side=tk.TOP, anchor="w")

    def _build_plot(self):
        f = ttk.Frame(self)
        f.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        bar = ttk.Frame(f)
        bar.pack(side=tk.TOP, fill=tk.X, padx=8)
        ttk.Label(bar, text="Finestra:").pack(side=tk.LEFT)
        self.win_var = tk.StringVar(value="1 h")
        cb = ttk.Combobox(bar, textvariable=self.win_var, values=list(WINDOWS),
                          width=7, state="readonly")
        cb.pack(side=tk.LEFT, padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self._redraw())

        self.fig = Figure(figsize=(10, 6), dpi=100)
        self.ax_t = self.fig.add_subplot(2, 1, 1)
        self.ax_h = self.fig.add_subplot(2, 1, 2, sharex=self.ax_t)
        self.lines = {}
        for key, lab, col, ls in TEMP_TRACES:
            ds = "steps-post" if key == "sp_active" else "default"
            lw = 2.0 if key in ("sp_active", "Td") else 1.3
            (self.lines[key],) = self.ax_t.plot([], [], ls, color=col, label=lab,
                                                drawstyle=ds, lw=lw)
        (self.lines["RH1"],) = self.ax_h.plot([], [], "-", color="tab:cyan", label="RH ambiente")
        self.ax_t.set_ylabel("Temperatura [°C]")
        self.ax_h.set_ylabel("RH [%]")
        for ax in (self.ax_t, self.ax_h):
            ax.grid(True, alpha=0.3)
            ax.legend(loc="upper left", fontsize=8, ncol=3)
        self.ax_h.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        NavigationToolbar2Tk(self.canvas, f).update()

    def _build_log(self):
        self.log_box = scrolledtext.ScrolledText(self, height=6, font=("Consolas", 9),
                                                 state=tk.DISABLED)
        self.log_box.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=4)

    # ------------------------------------------------------------ helpers
    def _log(self, msg):
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, f"{datetime.now():%H:%M:%S}  {msg}\n")
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_box.delete("1.0", f"{lines - 2000}.0")
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _set_mode(self, mode):
        self.mode_lbl.configure(text=mode, bg=MODE_COLORS.get(mode, "#455a64"))

    def _set_running(self, running):
        st = tk.DISABLED if running else tk.NORMAL
        for e in self.entries:
            e.configure(state=st)
        self.dry_chk.configure(state=st)
        if running:
            self.btn.configure(text="■  STOP", bg="#c62828")
        else:
            self.btn.configure(text="▶  START", bg="#2e7d32", state=tk.NORMAL)
            self._set_mode("STOP")

    # ------------------------------------------------------- start / stop
    def toggle(self):
        if self.worker and self.worker.is_alive():
            self.stop()
        else:
            self.start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            v = {k: s.get().strip() for k, s in self.vars.items()}
            cfg = cg.Config(margin=float(v["margin"]), surf_margin=float(v["surf_margin"]),
                            sp_min=float(v["sp_min"]), sp_max=float(v["sp_max"]),
                            failsafe_sp=float(v["failsafe_sp"]))
            interval = float(v["interval"])
        except ValueError as e:
            messagebox.showerror("Parametri", f"Valore non valido: {e}")
            return
        dry = self.dry_var.get()
        log = lambda m: self.q.put(("log", m))
        runner = cg.GuardRunner(v["port"], v["thor_ip"], cfg, baud=self.args.baud,
                                rtscts=self.args.rtscts, interval=interval, dry_run=dry,
                                exit_sp=self.args.exit_sp, log_dir=self.args.log_dir, log=log)
        self.stop_event.clear()

        def work():
            err = None
            try:
                runner.run(self.stop_event, on_row=lambda r: self.q.put(("row", r)))
            except Exception as e:          # serial port busy, chiller not answering, ...
                err = f"{type(e).__name__}: {e}"
            self.q.put(("end", err))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_running(True)
        self._log(f"Avvio: porta {v['port']}, Thor {v['thor_ip']}"
                  f"{'  [DRY RUN]' if dry else ''}")

    def stop(self):
        self.stop_event.set()
        self.btn.configure(text="arresto…", state=tk.DISABLED)
        self._log("Arresto in corso…")

    # ------------------------------------------------------------- polling
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "row":
                    self._on_row(payload)
                elif kind == "log":
                    self._log(payload)
                elif kind == "end":
                    self._set_running(False)
                    if payload:
                        self._log(f"ERRORE: {payload}")
                        if not self.closing:
                            messagebox.showerror("Condensation Guard", payload)
                    else:
                        self._log("Loop fermato.")
                    if self.closing:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        if self.dirty:
            self._redraw()
        self.after(500, self._poll)

    def _on_row(self, r):
        for k in self.data:
            self.data[k].append(r[k])
            if len(self.data[k]) > MAX_POINTS:
                del self.data[k][: len(self.data[k]) - MAX_POINTS]
        self.dirty = True
        for k, (var, unit) in self.readouts.items():
            x = r.get(k, math.nan)
            if isinstance(x, float) and math.isnan(x):
                var.set("--")
            else:
                fmt = "{:+.2f}" if k == "surf_margin" else ("{:.1f}" if k == "RH1" else "{:.2f}")
                var.set(f"{fmt.format(x)} {unit}")
        self._set_mode(r["mode"])
        self.stat_var.set(f"Ultimo aggiornamento {r['time']}   |   stato LAUDA: {r['lauda_stat'] or '--'}")

    def _redraw(self):
        self.dirty = False
        ts = self.data["ts"]
        if not ts:
            return
        minutes = WINDOWS[self.win_var.get()]
        i0 = 0
        if minutes is not None:
            t_lo = ts[-1] - timedelta(minutes=minutes)
            while i0 < len(ts) and ts[i0] < t_lo:   # cheap for typical sizes
                i0 += 1
        x = ts[i0:]
        for k, ln in self.lines.items():
            ln.set_data(x, self.data[k][i0:])
        for ax in (self.ax_t, self.ax_h):
            ax.relim()
            ax.autoscale_view()
        lo = x[0] if minutes is None else ts[-1] - timedelta(minutes=minutes)
        if lo < ts[-1]:
            self.ax_t.set_xlim(lo, ts[-1] + timedelta(seconds=5))
        self.canvas.draw_idle()

    # --------------------------------------------------------------- close
    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Chiudi", "Il loop anti-condensa è attivo.\n"
                                       "Fermarlo e chiudere? (il chiller resta all'ultimo set point)"):
                return
            self.closing = True
            self.stop()          # window is destroyed when the worker reports "end"
        else:
            self.destroy()


def main():
    p = cg.build_parser()
    p.add_argument("--autostart", action="store_true", help="start the loop at launch")
    args = p.parse_args()
    app = GuardGUI(args)
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # under pythonw there is no console: show the error
        import traceback
        r = tk.Tk(); r.withdraw()
        messagebox.showerror("Condensation Guard", traceback.format_exc())
        raise
