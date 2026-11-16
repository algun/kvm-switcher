"""macOS LaunchAgent that runs the watcher at login."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from kvm_switcher import install_root
from kvm_switcher.service import parse_launchctl_list

LABEL = "com.kvm-switcher.watcher"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def plist_text() -> str:
    """The LaunchAgent property list, one ProgramArguments entry per argv item."""
    arguments = "\n".join(
        "        <string>{}</string>".format(_xml(part))
        for part in install_root.program_argv()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>{label}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        "{arguments}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <true/>\n"
        "</dict>\n"
        "</plist>\n"
    ).format(label=LABEL, arguments=arguments)


def _xml(text: str) -> str:
    """Escape the five characters XML reserves, so an odd path cannot break the plist."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def install() -> None:
    plist = plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_text(), encoding="utf-8")
    subprocess.run(["launchctl", "load", str(plist)], check=False)


def uninstall() -> bool:
    """Unload and remove the LaunchAgent. True when something was removed."""
    plist = plist_path()
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", "gui/{}".format(uid), str(plist)], check=False)
    subprocess.run(["launchctl", "unload", str(plist)], check=False)
    removed = False
    if plist.is_file():
        plist.unlink()
        removed = True
    return removed


def installed() -> bool:
    return plist_path().is_file()


def running() -> bool:
    result = subprocess.run(
        ["launchctl", "list", LABEL],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return parse_launchctl_list(result.stdout)
