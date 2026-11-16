"""Windows monitor discovery and switching through DXVA2.

Everything here is imported lazily by ``displays.__init__``, and the
Windows-only ctypes entry points (``WinDLL``, ``WINFUNCTYPE``) are bound inside
functions so importing this module on Linux or macOS cannot fail.
"""

from __future__ import annotations

import ctypes
import subprocess
from typing import List, Tuple

from kvm_switcher.devices.windows import useful_device_name
from kvm_switcher.displays import VCP_INPUT, FoundDisplay, monitor_matches
from kvm_switcher.mccs import parse_mccs_inputs

# Windows reports many panels as a placeholder instead of the real model name,
# so a monitor friendly name saved from another OS can never match.
_WINDOWS_GENERIC_DESCRIPTIONS = frozenset(
    {"generic pnp monitor", "generic monitor", "pnp monitor"}
)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_void_p),
        ("description", ctypes.c_wchar * 128),
    ]


# Built lazily: ctypes.WINFUNCTYPE exists only on Windows, and importing this
# module on Linux or macOS must not fail.
_WINDOWS_MONITOR_CALLBACK_TYPE = None


def _windows_monitor_callback_type():
    global _WINDOWS_MONITOR_CALLBACK_TYPE
    if _WINDOWS_MONITOR_CALLBACK_TYPE is None:
        _WINDOWS_MONITOR_CALLBACK_TYPE = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_RECT),
            ctypes.c_ssize_t,
        )
    return _WINDOWS_MONITOR_CALLBACK_TYPE


def windows_monitor_matches(description: str, wanted: str) -> bool:
    """True when a description is a match, or is one of Windows' placeholders."""
    if monitor_matches(description, wanted):
        return True
    return description.strip().casefold() in _WINDOWS_GENERIC_DESCRIPTIONS


def _windows_physical_monitors():
    """(dxva2, [(description, handle, array, count)]) for every physical monitor."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
    collected: List[Tuple[str, ctypes.c_void_p, object, ctypes.c_uint]] = []

    def _callback(hmonitor, _hdc, _rect, _data):
        count = ctypes.c_uint()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)):
            return 1
        if count.value == 0:
            return 1
        monitors = (_PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count, monitors):
            return 1
        for monitor in monitors:
            collected.append((monitor.description, monitor.handle, monitors, count))
        return 1

    callback = _windows_monitor_callback_type()(_callback)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    return dxva2, collected


def _release_windows_monitors(dxva2, collected) -> None:
    """One DestroyPhysicalMonitors call covers the array every entry in it shares."""
    destroyed = set()
    for _description, _handle, array, count in collected:
        marker = id(array)
        if marker in destroyed:
            continue
        destroyed.add(marker)
        dxva2.DestroyPhysicalMonitors(count, array)


def windows_capabilities(dxva2, handle) -> str:
    """The MCCS capability string a monitor reports, or "" when it gives none.

    The length from GetCapabilitiesStringLength counts bytes of an ANSI reply, and
    CapabilitiesRequestAndCapabilitiesReply takes a byte buffer. Handing it a
    wchar buffer instead makes Windows treat the two-byte characters as the
    payload and interleave every character into mojibake.
    """
    length = ctypes.c_uint()
    if not dxva2.GetCapabilitiesStringLength(handle, ctypes.byref(length)) or length.value == 0:
        return ""
    buffer = ctypes.create_string_buffer(length.value)
    if not dxva2.CapabilitiesRequestAndCapabilitiesReply(handle, buffer, length.value):
        return ""
    return buffer.value.decode("ascii", "replace")


def _windows_monitor_model_name() -> str:
    """Real monitor model from WMI, since DXVA2 only reports "Generic PnP Monitor"."""
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorID "
                "-ErrorAction SilentlyContinue | ForEach-Object { "
                "$n = ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object {[char]$_}) -join ''; "
                "if ($n) { $n } }",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in output.splitlines():
        name = line.strip()
        if useful_device_name(name):
            return name
    return ""


def discover() -> List[FoundDisplay]:
    """Windows has no ddcutil, so ask each monitor for its own input list."""
    dxva2, collected = _windows_physical_monitors()
    displays: List[FoundDisplay] = []
    try:
        for description, handle, _array, _count in collected:
            display = FoundDisplay(name=description or "Monitor")
            display.inputs = parse_mccs_inputs(windows_capabilities(dxva2, handle))
            displays.append(display)
    finally:
        _release_windows_monitors(dxva2, collected)
    # DXVA2 reports every panel as "Generic PnP Monitor"; WMI knows the model, and
    # the name is only used for display, never for matching a switch target.
    model = _windows_monitor_model_name()
    if model:
        for display in displays:
            if display.name.strip().casefold() in _WINDOWS_GENERIC_DESCRIPTIONS:
                display.name = model
    return displays


def _switch_windows(value: int, name: str = "") -> int:
    """Write the input value to every monitor that matches, and count them."""
    dxva2, collected = _windows_physical_monitors()
    try:
        # Windows usually reports "Generic PnP Monitor", which cannot be matched
        # against a real model name. Falling back to a generic monitor is only
        # safe when there is exactly one: with several, a saved name that matches
        # nothing would switch unrelated displays.
        allow_generic = len(collected) == 1
        switched = 0
        for description, handle, _array, _count in collected:
            if not monitor_matches(description, name):
                if not (allow_generic and windows_monitor_matches(description, name)):
                    continue
            if dxva2.SetVCPFeature(handle, VCP_INPUT, value):
                switched += 1
        return switched
    finally:
        _release_windows_monitors(dxva2, collected)


def switch(value: int, name: str = "") -> int:
    """The dispatch target `displays.switch_monitor` calls on Windows."""
    return _switch_windows(value, name)
