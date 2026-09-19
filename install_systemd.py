#!/usr/bin/env python3
"""Install the existing user service as the sole periodic reconciler."""
from __future__ import annotations

import getpass
from pathlib import Path
import shutil
import subprocess

import install_global_command


ROOT = Path(__file__).resolve().parent
UNITS = ("github-autosync.service", "github-autosync.timer", "repo-integrator.service", "repo-integrator.timer")


def checked(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def main() -> int:
    install_global_command.main()
    target = Path.home() / ".config" / "systemd" / "user"
    target.mkdir(parents=True, exist_ok=True)
    for name in UNITS:
        source = ROOT / "systemd" / name
        destination = target / name
        if not destination.exists() or destination.read_bytes() != source.read_bytes():
            shutil.copyfile(source, destination)
    checked("systemctl", "--user", "daemon-reload")
    for legacy in ("codex-github-autosync.timer",):
        subprocess.run(["systemctl", "--user", "disable", "--now", legacy],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    checked("systemctl", "--user", "enable", "--now", "github-autosync.timer")
    checked("systemctl", "--user", "enable", "--now", "repo-integrator.timer")
    # Fedora permits an ordinary user to enable their own lingering when logind
    # policy allows it; leave the installed timer intact if policy rejects it.
    linger = subprocess.run(["loginctl", "enable-linger", getpass.getuser()],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    print("github-autosync.timer + repo-integrator.timer active; linger " + ("enabled" if linger.returncode == 0 else "requires administrator"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
