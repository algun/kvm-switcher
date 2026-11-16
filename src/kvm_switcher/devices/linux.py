"""Linux USB device enumeration from ``lsusb``."""

from __future__ import annotations

import re
import subprocess
from typing import List, Tuple

_LSUSB_RE = re.compile(r"ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4}) (.*)")
# Linux USB root hubs. They are always present and never a KVM trigger.
_LINUX_ROOT_HUB = "1d6b:"


def parse_lsusb(output: str) -> List[Tuple[str, str]]:
    """(vid:pid, description), skipping root hubs and repeated ids."""
    found: List[Tuple[str, str]] = []
    seen = set()
    for line in output.splitlines():
        match = _LSUSB_RE.search(line)
        if not match:
            continue
        vidpid = "{}:{}".format(match.group(1), match.group(2)).lower()
        if vidpid.startswith(_LINUX_ROOT_HUB) or vidpid in seen:
            continue
        seen.add(vidpid)
        found.append((vidpid, match.group(3).strip()))
    return found


def connected_devices() -> List[Tuple[str, str]]:
    """Present USB devices, or [] when lsusb is unavailable."""
    try:
        output = subprocess.check_output(["lsusb"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_lsusb(output)
