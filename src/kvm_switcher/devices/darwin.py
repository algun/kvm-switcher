"""macOS USB device enumeration from ``ioreg``."""

from __future__ import annotations

import subprocess
from typing import List, Tuple

IOREG = "/usr/sbin/ioreg"


def parse_ioreg(output: str) -> List[Tuple[str, str]]:
    """(vid:pid, device name) from `ioreg -p IOUSB -l -w0 -x`."""
    found: List[Tuple[str, str]] = []
    seen = set()
    name = ""
    vendor = ""
    product = ""

    def flush() -> None:
        nonlocal vendor, product
        if not vendor or not product:
            vendor = product = ""
            return
        try:
            vidpid = "{:04x}:{:04x}".format(int(vendor, 16), int(product, 16))
        except ValueError:
            vendor = product = ""
            return
        vendor = product = ""
        if vidpid in seen:
            return
        seen.add(vidpid)
        found.append((vidpid, name or vidpid))

    for line in output.splitlines():
        if "+-o " in line:
            flush()
            chunk = line.split("+-o ", 1)[1]
            name = chunk.split("@", 1)[0].strip() or ""
            continue
        stripped = line.strip()
        if stripped.startswith('"idVendor"'):
            vendor = stripped.split("=", 1)[-1].strip()
        elif stripped.startswith('"idProduct"'):
            product = stripped.split("=", 1)[-1].strip()
    flush()
    return found


def connected_devices() -> List[Tuple[str, str]]:
    """Present USB devices, or [] when ioreg is unavailable."""
    try:
        output = subprocess.check_output(
            [IOREG, "-p", "IOUSB", "-l", "-w0", "-x"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_ioreg(output)
