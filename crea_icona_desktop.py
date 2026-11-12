# encoding: utf-8
"""
Creates the "Condensation Guard" icon on the Windows desktop.

Run it ONCE with the same Python (or conda env) you'll use for the program:
    python crea_icona_desktop.py                  # loop starts when opened
    python crea_icona_desktop.py --no-autostart   # opens the GUI only, then press START
    python crea_icona_desktop.py --dry-run        # icon in test mode (never writes the chiller)

Generates Avvia_Condensation_Guard.bat next to the scripts and a .lnk on the desktop
pointing to it. If Python is a conda env, the .bat activates it first.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI = os.path.join(HERE, "condensation_guard_gui.py")
BAT = os.path.join(HERE, "Avvia_Condensation_Guard.bat")


def find_conda_activate():
    """Return (activate.bat, env prefix) if this Python lives in a conda env."""
    prefix = sys.prefix
    if not os.path.isdir(os.path.join(prefix, "conda-meta")):
        return None
    candidates = []
    if os.environ.get("CONDA_EXE"):
        candidates.append(os.path.dirname(os.path.dirname(os.environ["CONDA_EXE"])))
    parent = os.path.dirname(prefix)
    if os.path.basename(parent).lower() == "envs":
        candidates.append(os.path.dirname(parent))
    candidates.append(prefix)                       # base env
    for base in candidates:
        act = os.path.join(base, "Scripts", "activate.bat")
        if os.path.isfile(act):
            return act, prefix
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default="socket://192.168.30.103:4660")
    p.add_argument("--thor-ip", default="192.168.30.134")
    p.add_argument("--no-autostart", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--name", default="Condensation Guard")
    a = p.parse_args()

    if os.name != "nt":
        sys.exit("This script is only for Windows.")

    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = sys.executable

    extra = f'--port {a.port} --thor-ip {a.thor_ip}'
    if not a.no_autostart:
        extra += " --autostart"
    if a.dry_run:
        extra += " --dry-run"

    lines = ["@echo off", f'cd /d "{HERE}"']
    conda = find_conda_activate()
    if conda:
        lines.append(f'call "{conda[0]}" "{conda[1]}"')
    lines.append(f'start "" "{pyw}" "{GUI}" {extra} %*')
    with open(BAT, "w", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Launcher: {BAT}")

    q = lambda x: x.replace("'", "''")   # escape for PowerShell single quotes
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        f"$l=Join-Path $d '{q(a.name)}.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($l);"
        f"$s.TargetPath='{q(BAT)}';"
        f"$s.WorkingDirectory='{q(HERE)}';"
        "$s.WindowStyle=7;"                         # minimized: no console flash
        f"$s.IconLocation='{q(pyw)},0';"
        "$s.Description='Anti-condensation loop LAUDA Microcool + Thor';"
        "$s.Save(); Write-Output $l"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Shortcut creation failed:\n{r.stderr}")
    print(f"Desktop icon: {r.stdout.strip()}")


if __name__ == "__main__":
    main()
