"""USB device enumeration, one module per platform.

The public functions here dispatch to the private module for the current
platform. Each private module imports its platform-only dependencies inside
the functions that need them, so importing this package on any OS is safe.
"""

from __future__ import annotations

from typing import List, Tuple

from kvm_switcher import paths

__all__ = ["connected_devices", "catalog"]


def connected_devices() -> List[Tuple[str, str]]:
    """Every USB device present now, as (vid:pid, name) in scan order."""
    # Imported here, not at module scope: `devices.windows` pulls in ctypes
    # structures and `devices.darwin` shells out, and neither should load on a
    # platform that will never call it.
    key = paths.platform_key()
    if key == "linux":
        from kvm_switcher.devices import linux

        return linux.connected_devices()
    if key == "darwin":
        from kvm_switcher.devices import darwin

        return darwin.connected_devices()
    if key == "windows":
        from kvm_switcher.devices import windows

        return windows.connected_devices()
    return []


def catalog(saved: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Present devices plus saved ones that are unplugged, labelled as such."""
    items = list(connected_devices())
    present = {vidpid for vidpid, _ in items}
    for vidpid, name in saved:
        if vidpid in present:
            continue
        label = name if name.endswith("(not connected)") else name + " (not connected)"
        items.append((vidpid, label))
    return items
