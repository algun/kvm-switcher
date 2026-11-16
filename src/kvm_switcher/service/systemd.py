"""Linux ``systemd --user`` unit that runs the watcher at login."""

from __future__ import annotations

import subprocess
from pathlib import Path

from kvm_switcher import install_root
from kvm_switcher.service import parse_systemctl_is_active

UNIT_NAME = "kvm-switcher.service"


def unit_path() -> Path:
    return Path.home() / ".config/systemd/user" / UNIT_NAME


def unit_text() -> str:
    """The unit file contents, pointing at this installation."""
    return "\n".join([
        "[Unit]",
        "Description=KVM Switcher",
        "After=graphical.target",
        "",
        "[Service]",
        "ExecStart={}".format(subprocess.list2cmdline(install_root.program_argv())),
        "Restart=always",
        "RestartSec=1",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def install() -> None:
    unit = unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=False)


def uninstall() -> bool:
    """Disable and remove the unit. True when something was actually removed."""
    unit = unit_path()
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    removed = False
    if unit.is_file():
        unit.unlink()
        removed = True
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return removed


def installed() -> bool:
    return unit_path().is_file()


def running() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", UNIT_NAME],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return parse_systemctl_is_active(result.stdout)
