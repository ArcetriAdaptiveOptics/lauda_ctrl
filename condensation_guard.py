# encoding: utf-8
"""
condensation_guard.py - Anti-condensation loop for a LAUDA Microcool (MC 1200)
chiller, driven by a Thor T/RH sensor.

Thor probes (see https://github.com/ArcetriAdaptiveOptics/Thor):
    port 1 : ambient air          -> T_amb, RH_amb -> dew point Td
    port 2 : refrigerated chassis -> T_chassis (surface)
    port 3 : inlet cooling tube   -> T_tube    (surface)

Control law (feedforward + feedback):
    SP_ff   = Td + margin                                  (feedforward)
    m       = min(T_chassis, T_tube) - Td                  (surface margin)
    bump   += ki * (surf_margin - m) * dt   if m < surf_margin
    bump   -= leak * dt                     otherwise      (slow decay)
    SP_cmd  = clamp(SP_ff + bump, sp_min, sp_max)
    SP_cmd is rate-limited (fast up, slow down), rounded to 0.1 C and only
    written when it differs from the active set point by > deadband.

Fail-safe: after N consecutive bad Thor readings the set point goes to
--failsafe-sp (warm enough to rule out condensation).

Chiller: RS 232, 8N1, no parity, CRLF; commands from the operation manual
(Q4DA-E_13-039 V1R26, ch. 6.8): OUT_SP_00_XX.X, IN_SP_00, IN_PV_00,
IN_SP_04 (Hi), IN_SP_05 (Lo), STATUS, STAT, TYPE.

Usage (Windows):
    python condensation_guard.py --check                # test Thor + chiller only
    python condensation_guard.py --dry-run              # loop, never writes
    python condensation_guard.py                        # loop (Moxa 192.168.30.103, Thor 192.168.30.134)
    python condensation_guard_gui.py                    # GUI with plots
"""

__author__ = "Marco Bonaglia"
__version__ = "0.2.0"

import argparse
import csv
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

import requests
import serial

DEFAULT_PORT = "socket://192.168.30.103:4660"   # Moxa NPort 5110A (or "COM3")
DEFAULT_THOR_IP = "192.168.30.134"
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
NAN = float("nan")


# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------
# Magnus formula, Sonntag 1990 coefficients over water (-45..60 C, err < 0.35 C)
_A, _B = 17.62, 243.12


def dew_point(t_c, rh_pct):
    """Dew point [C] from air temperature [C] and relative humidity [%]."""
    rh = min(max(rh_pct, 0.1), 100.0)
    g = math.log(rh / 100.0) + _A * t_c / (_B + t_c)
    return _B * g / (_A - g)


# ---------------------------------------------------------------------------
# Thor reader (same endpoint/format as pythor.get_thor_sensor_data)
# ---------------------------------------------------------------------------
def read_thor(ip, timeout=3.0, log=print):
    """Return {port: (temp, hum)} with NaN for missing values; {} on error.

    Thor answers <...><sensors><sensor><port/><connected/><type/><temp/><hum/>
    with "N/A" for missing readings; disconnected probes are reported as NaN.
    """
    try:
        r = requests.get(f"http://{ip}/sensors.dxl", timeout=timeout)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        log(f"[thor] read error: {e}")
        return {}

    def _f(node):
        try:
            v = float(node.text)
        except (TypeError, ValueError, AttributeError):   # "N/A", missing tag
            return NAN
        return v if math.isfinite(v) else NAN

    out = {}
    for s in root.iter("sensor"):
        try:
            port = int(s.find("port").text)
        except (TypeError, ValueError, AttributeError):
            continue
        conn = s.find("connected")
        if conn is not None and (conn.text or "").strip() == "0":
            out[port] = (NAN, NAN)
            continue
        out[port] = (_f(s.find("temp")), _f(s.find("hum")))
    return out


# ---------------------------------------------------------------------------
# LAUDA Microcool RS 232 driver
# ---------------------------------------------------------------------------
class LaudaError(RuntimeError):
    pass


_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class Microcool:
    def __init__(self, port, baudrate=9600, timeout=2.0, rtscts=False):
        # port can be "COM3" or a pyserial URL such as
        # "socket://192.168.30.103:4660" (Moxa NPort in TCP Server mode;
        # baud/handshake are then set on the NPort, not here).
        self.ser = serial.serial_for_url(port, baudrate=baudrate, bytesize=8,
                                         parity=serial.PARITY_NONE, stopbits=1,
                                         timeout=timeout, rtscts=rtscts)
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def query(self, cmd, retries=2):
        """Send one command and wait for its CR/LF-terminated reply."""
        last = None
        for _ in range(retries + 1):
            self.ser.reset_input_buffer()
            self.ser.write((cmd + "\r\n").encode("ascii"))
            # accept CR, LF or CRLF as terminator
            reply = self.ser.read_until(b"\n").decode("ascii", "replace").strip()
            if not reply:
                reply = self.ser.read_until(b"\r").decode("ascii", "replace").strip()
            if reply and not reply.upper().startswith("ERR"):
                return reply
            last = reply or "timeout (no reply)"
            time.sleep(0.3)
        raise LaudaError(f"{cmd!r} -> {last}")

    def _num(self, cmd):
        reply = self.query(cmd)
        m = _NUM_RE.search(reply)
        if not m:
            raise LaudaError(f"{cmd!r} -> non-numeric reply {reply!r}")
        return float(m.group())

    # read
    def device_type(self):
        return self.query("TYPE")

    def setpoint(self):
        return self._num("IN_SP_00")

    def outflow_temp(self):
        return self._num("IN_PV_00")

    def limit_hi(self):
        return self._num("IN_SP_04")

    def limit_lo(self):
        return self._num("IN_SP_05")

    def status_ok(self):
        return self.query("STATUS").strip() == "0"

    def fault_string(self):
        return self.query("STAT")

    # write
    def set_setpoint(self, value):
        reply = self.query(f"OUT_SP_00_{value:.1f}")
        if reply.upper() != "OK":
            raise LaudaError(f"set point {value:.1f} refused: {reply}")


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
@dataclass
class Config:
    margin: float = 2.0        # SP_ff = Td + margin                     [C]
    surf_margin: float = 1.0   # min allowed (T_surface - Td)            [C]
    ki: float = 0.02           # integral gain on surface margin         [1/s]
    leak: float = 0.002        # bump decay when surfaces are safe       [C/s]
    bump_max: float = 5.0      # max feedback correction                 [C]
    rate_up: float = 0.10      # max SP increase                         [C/s]
    rate_down: float = 0.005   # max SP decrease (0.3 C/min)             [C/s]
    deadband: float = 0.15     # min change before writing SP            [C]
    alpha: float = 0.3         # EMA smoothing on ambient T and RH
    sp_min: float = 8.0        # user clamp (also >= device Lo + 2)      [C]
    sp_max: float = 25.0       # user clamp (also <= device Hi - 2)      [C]
    failsafe_sp: float = 22.0  # SP if Thor data is lost                 [C]
    max_bad: int = 3           # consecutive bad reads before fail-safe


class CondensationGuard:
    def __init__(self, cfg, sp_min, sp_max, sp_now):
        self.cfg = cfg
        self.sp_min, self.sp_max = sp_min, sp_max
        self.sp = self._clamp(sp_now)   # last commanded (rate-limited, unrounded)
        self.bump = 0.0
        self.t_f = self.rh_f = None
        self.bad = 0

    def _clamp(self, x):
        return min(max(x, self.sp_min), self.sp_max)

    def step(self, data, dt):
        """Return (sp_target, info dict). data = {port: (T, RH)}."""
        c = self.cfg
        t_amb, rh_amb = data.get(1, (NAN, NAN))
        t_ch = data.get(2, (NAN,))[0]
        t_tb = data.get(3, (NAN,))[0]

        if math.isnan(t_amb) or math.isnan(rh_amb):
            self.bad += 1
            if self.bad >= c.max_bad:
                self.sp = self._clamp(c.failsafe_sp)
                return self.sp, {"mode": "FAILSAFE"}
            return self.sp, {"mode": "HOLD"}
        self.bad = 0

        # smoothed ambient -> dew point
        if self.t_f is None:
            self.t_f, self.rh_f = t_amb, rh_amb
        else:
            self.t_f += c.alpha * (t_amb - self.t_f)
            self.rh_f += c.alpha * (rh_amb - self.rh_f)
        td = dew_point(self.t_f, self.rh_f)

        # feedback on the coldest monitored surface
        surfaces = [t for t in (t_ch, t_tb) if not math.isnan(t)]
        m = (min(surfaces) - td) if surfaces else NAN
        if not math.isnan(m) and m < c.surf_margin:
            self.bump += c.ki * (c.surf_margin - m) * dt
        else:
            self.bump -= c.leak * dt
        self.bump = min(max(self.bump, 0.0), c.bump_max)

        target = self._clamp(td + c.margin + self.bump)

        # asymmetric rate limit: warm up fast, cool down slowly
        d = target - self.sp
        d = min(d, c.rate_up * dt) if d > 0 else max(d, -c.rate_down * dt)
        self.sp = self._clamp(self.sp + d)

        mode = "FB" if self.bump > 0.05 else "FF"
        return self.sp, {"mode": mode, "td": td, "surf_margin": m,
                         "target": target, "bump": self.bump}


# ---------------------------------------------------------------------------
# Runner: owns chiller + guard + CSV log; used by both CLI and GUI
# ---------------------------------------------------------------------------
class GuardRunner:
    FIELDS = ["time", "T1", "RH1", "T2", "T3", "Td", "surf_margin", "bump",
              "target", "sp_cmd", "sp_active", "pv", "mode", "lauda_stat"]

    def __init__(self, port=DEFAULT_PORT, thor_ip=DEFAULT_THOR_IP, cfg=None,
                 baud=9600, rtscts=False, interval=10.0, dry_run=False,
                 exit_sp=None, log_dir=DEFAULT_LOG_DIR, log=print):
        self.port, self.thor_ip = port, thor_ip
        self.cfg = cfg or Config()
        self.baud, self.rtscts = baud, rtscts
        self.interval = max(1.0, float(interval))
        self.dry_run, self.exit_sp = dry_run, exit_sp
        self.log_dir, self.log = log_dir, log
        self.chiller = None
        self.guard = None
        self._f_log = None
        self._w = None

    # -- setup / teardown -----------------------------------------------
    def open(self):
        self.chiller = Microcool(self.port, self.baud, rtscts=self.rtscts)
        try:
            self.log(f"[lauda] type={self.chiller.device_type()}  "
                     f"SP={self.chiller.setpoint():.2f}  PV={self.chiller.outflow_temp():.2f}")
            lo, hi = self.chiller.limit_lo(), self.chiller.limit_hi()
            # manual 6.6: usable SP range is [Lo + 2, Hi - 2]
            self.sp_min = max(self.cfg.sp_min, lo + 2.0)
            self.sp_max = min(self.cfg.sp_max, hi - 2.0)
            if self.sp_min > self.sp_max:
                raise LaudaError(f"Empty SP range: device Lo/Hi = {lo}/{hi}, "
                                 f"user {self.cfg.sp_min}/{self.cfg.sp_max}")
            self.log(f"[lauda] limits Lo={lo} Hi={hi} -> SP range "
                     f"[{self.sp_min:.1f}, {self.sp_max:.1f}]")
            self.sp_active = self.chiller.setpoint()
        except Exception:
            self.chiller.close()
            self.chiller = None
            raise
        self.guard = CondensationGuard(self.cfg, self.sp_min, self.sp_max, self.sp_active)

        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(self.log_dir, f"guard_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self._f_log = open(self.log_path, "w", newline="")
        self._w = csv.DictWriter(self._f_log, fieldnames=self.FIELDS, extrasaction="ignore")
        self._w.writeheader()
        self.log(f"[log] {self.log_path}  {'(DRY RUN: chiller is never written)' if self.dry_run else ''}")

    def close(self):
        if self.chiller is not None:
            if self.exit_sp is not None and not self.dry_run:
                try:
                    self.chiller.set_setpoint(min(max(self.exit_sp, self.sp_min), self.sp_max))
                    self.log(f"[lauda] exit SP -> {self.chiller.setpoint():.1f}")
                except Exception as e:
                    self.log(f"[lauda] could not set exit SP: {e}")
            self.chiller.close()
            self.chiller = None
        if self._f_log is not None:
            self._f_log.close()
            self._f_log = None

    # -- one control cycle ------------------------------------------------
    def step(self, dt):
        data = read_thor(self.thor_ip, log=self.log)
        sp_cmd, info = self.guard.step(data, dt)
        sp_round = round(sp_cmd, 1)

        stat, pv = "", NAN
        try:
            pv = self.chiller.outflow_temp()
            stat = self.chiller.fault_string()
            diff = abs(sp_round - self.sp_active)
            if (diff > self.cfg.deadband or info["mode"] == "FAILSAFE") and diff >= 0.05:
                if not self.dry_run:
                    self.chiller.set_setpoint(sp_round)
                    self.sp_active = self.chiller.setpoint()
                else:
                    self.sp_active = sp_round
        except (LaudaError, serial.SerialException, ValueError) as e:
            self.log(f"[lauda] {e}")

        g = lambda port, i: data.get(port, (NAN, NAN))[i]
        now = datetime.now()
        row = {"ts": now, "time": now.isoformat(timespec="seconds"),
               "T1": g(1, 0), "RH1": g(1, 1), "T2": g(2, 0), "T3": g(3, 0),
               "Td": info.get("td", NAN),
               "surf_margin": info.get("surf_margin", NAN),
               "bump": info.get("bump", NAN),
               "target": info.get("target", NAN),
               "sp_cmd": sp_cmd, "sp_active": self.sp_active, "pv": pv,
               "mode": info["mode"], "lauda_stat": stat}
        self._w.writerow({k: (f"{v:.2f}" if isinstance(v, float) else v)
                          for k, v in row.items() if k in self.FIELDS})
        self._f_log.flush()
        return row

    @staticmethod
    def format_row(row):
        return (f"{row['time']} [{row['mode']:8s}] Tamb={row['T1']:.2f} RH={row['RH1']:.1f} "
                f"Td={row['Td']:.2f} | Tch={row['T2']:.2f} Ttube={row['T3']:.2f} "
                f"m={row['surf_margin']:+.2f} bump={row['bump']:.2f} | "
                f"SP={row['sp_active']:.1f} PV={row['pv']:.2f} stat={row['lauda_stat']}")

    # -- loop -----------------------------------------------------------
    def run(self, stop_event=None, on_row=None):
        """Run until stop_event is set (or Ctrl+C). Opens and closes everything."""
        self.open()
        t_prev = time.monotonic() - self.interval   # first dt = one period
        try:
            while stop_event is None or not stop_event.is_set():
                t0 = time.monotonic()
                dt, t_prev = t0 - t_prev, t0
                dt = dt if dt > 0 else self.interval
                row = self.step(dt)
                if on_row:
                    on_row(row)
                else:
                    self.log(self.format_row(row))
                # interruptible wait (short sleeps also keep Ctrl+C working on Windows)
                end = t0 + self.interval
                while time.monotonic() < end:
                    if stop_event is not None and stop_event.is_set():
                        break
                    time.sleep(min(0.2, max(0.0, end - time.monotonic())))
        finally:
            self.close()


# ---------------------------------------------------------------------------
# Connection check
# ---------------------------------------------------------------------------
def check_hardware(port, thor_ip, baud=9600, rtscts=False, log=print):
    ok = True
    log(f"--- Thor @ {thor_ip}")
    data = read_thor(thor_ip, log=log)
    if not data:
        ok = False
    for p in sorted(data):
        t, h = data[p]
        log(f"    port {p}: T={t:.2f} C  RH={h:.1f} %")
    if data and not math.isnan(data.get(1, (NAN, NAN))[1]):
        t, h = data[1]
        log(f"    dew point (port 1) = {dew_point(t, h):.2f} C")
    elif data:
        log("    WARNING: port 1 has no T/RH -> the loop would go to FAILSAFE")
        ok = False

    log(f"--- LAUDA @ {port} ({baud} baud)")
    try:
        ch = Microcool(port, baud, rtscts=rtscts)
    except serial.SerialException as e:
        log(f"    cannot open {port}: {e}")
        return False
    try:
        for name, fn in [("TYPE", ch.device_type), ("IN_SP_00 (SP)", ch.setpoint),
                         ("IN_PV_00 (outflow)", ch.outflow_temp),
                         ("IN_SP_04 (Hi)", ch.limit_hi), ("IN_SP_05 (Lo)", ch.limit_lo),
                         ("STAT", ch.fault_string)]:
            try:
                log(f"    {name:20s} -> {fn()}")
            except (LaudaError, ValueError) as e:
                log(f"    {name:20s} -> ERROR {e}")
                ok = False
    finally:
        ch.close()
    log("--- OK" if ok else "--- some checks FAILED")
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="LAUDA Microcool anti-condensation loop")
    p.add_argument("--port", default=DEFAULT_PORT, help="COMx or socket://host:port (default: Moxa)")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--rtscts", action="store_true", help="hardware handshake")
    p.add_argument("--thor-ip", default=DEFAULT_THOR_IP)
    p.add_argument("--interval", type=float, default=10.0, help="loop period [s]")
    p.add_argument("--margin", type=float, default=2.0)
    p.add_argument("--surf-margin", type=float, default=1.0)
    p.add_argument("--sp-min", type=float, default=8.0)
    p.add_argument("--sp-max", type=float, default=25.0)
    p.add_argument("--failsafe-sp", type=float, default=22.0)
    p.add_argument("--exit-sp", type=float, default=None,
                   help="set point to leave on exit (default: leave last one)")
    p.add_argument("--dry-run", action="store_true", help="never write the chiller")
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    return p


def main():
    p = build_parser()
    p.add_argument("--check", action="store_true",
                   help="only test Thor and chiller communication, then exit")
    a = p.parse_args()

    if a.check:
        sys.exit(0 if check_hardware(a.port, a.thor_ip, a.baud, a.rtscts) else 1)

    cfg = Config(margin=a.margin, surf_margin=a.surf_margin, sp_min=a.sp_min,
                 sp_max=a.sp_max, failsafe_sp=a.failsafe_sp)
    runner = GuardRunner(a.port, a.thor_ip, cfg, a.baud, a.rtscts, a.interval,
                         a.dry_run, a.exit_sp, a.log_dir)
    try:
        runner.run()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except (LaudaError, serial.SerialException) as e:
        sys.exit(f"[lauda] {e}")


if __name__ == "__main__":
    main()
