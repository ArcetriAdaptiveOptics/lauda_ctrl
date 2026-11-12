# encoding: utf-8
"""
lauda_scan.py - Find the right serial settings for the LAUDA Microcool.

    python lauda_scan.py              # lists ports, then scans COM3
    python lauda_scan.py --port COM4  # scans another port

Tries baud rates x terminators x handshake, sends TYPE and IN_PV_00 and
prints the raw bytes received. Read-only: it never writes a set point.
"""

import argparse
import itertools
import time

import serial
import serial.tools.list_ports

BAUDS = [9600, 19200, 4800, 2400]
TERMS = {"CRLF": b"\r\n", "CR": b"\r"}
CMDS = ["TYPE", "IN_PV_00"]


def try_combo(port, baud, term, rtscts, timeout=1.0):
    try:
        s = serial.Serial(port, baud, bytesize=8, parity=serial.PARITY_NONE,
                          stopbits=1, timeout=timeout, rtscts=rtscts)
    except serial.SerialException as e:
        return None, f"cannot open: {e}"
    try:
        s.dtr = True
        s.rts = True
        time.sleep(0.2)
        s.reset_input_buffer()
        out = []
        for cmd in CMDS:
            s.reset_input_buffer()
            s.write(cmd.encode("ascii") + term)
            time.sleep(0.4)
            raw = s.read(64)
            out.append((cmd, raw))
        cts = s.cts if not rtscts else None
        return out, f"CTS={cts} DSR={s.dsr}"
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="COM3")
    a = p.parse_args()

    print("=== Serial ports on this PC")
    for pi in serial.tools.list_ports.comports():
        print(f"    {pi.device:6s} {pi.description}  [{pi.hwid}]")

    print(f"\n=== Scanning {a.port}")
    found = []
    for baud, (tname, term), rtscts in itertools.product(BAUDS, TERMS.items(), [False, True]):
        res, extra = try_combo(a.port, baud, term, rtscts)
        tag = f"{baud:5d} baud  {tname:4s}  rtscts={'on ' if rtscts else 'off'}"
        if res is None:
            print(f"{tag}  -> {extra}")
            return
        replies = "  ".join(f"{c}->{r!r}" for c, r in res)
        print(f"{tag}  {extra:22s} {replies}")
        if any(r for _, r in res):
            found.append(tag)

    print("\n=== Result")
    if found:
        print("Got bytes back with:\n    " + "\n    ".join(found))
    else:
        print("No bytes received with any setting -> check cable (straight vs null-modem),\n"
              "the RS 232 interface setting in the chiller menu, and that this is the right COM port.")


if __name__ == "__main__":
    main()
