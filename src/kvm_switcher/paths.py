"""Locations of the config file, state files, and install directories.

Pure path logic with no OS-specific behaviour and no I/O.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def platform_key() -> str:
    """A short name for the platform, matching the per-OS module names."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    return sys.platform


def config_path() -> Path:
    """The config file in use, honouring KVM_CONFIG and the legacy override."""
    override = os.environ.get("KVM_CONFIG") or os.environ.get("KVM_DEVICES_FILE")
    if override:
        return Path(override)
    if platform_key() == "windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "kvm-switcher" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "kvm-switcher" / "config.json"


def legacy_config_path(path: Path) -> Path:
    """The `devices.txt` a pre-JSON version wrote beside the config file."""
    return path.with_name("devices.txt")


def watcher_pid_path() -> Path:
    """Where the running watcher records its pid, so a second one can refuse."""
    return config_path().with_name("watcher.pid")


def config_destination(path: Path) -> Path:
    """The file `configure` writes: always the JSON config, never the legacy one."""
    if path.suffix.lower() == ".txt":
        return path.with_name("config.json")
    return path
