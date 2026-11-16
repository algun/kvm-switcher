"""Windows USB device enumeration (SetupAPI fallback alongside PowerShell).

The ctypes structures below are plain ``ctypes.Structure`` subclasses, which
exist on every platform. Only ``WinDLL`` is Windows-only, so it is bound inside
the function that runs on Windows and never at import time.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
from typing import Dict, List, Optional, Tuple

_VIDPID_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
_WINDOWS_HWID_RE = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)

_DIGCF_PRESENT = 0x02
_DIGCF_ALLCLASSES = 0x04
_SPDRP_DEVICEDESC = 0x00
_SPDRP_HARDWAREID = 0x01
_SPDRP_SERVICE = 0x04
_SPDRP_FRIENDLYNAME = 0x0C
_DEVPROP_TYPE_STRING = 0x12
_DEVPROP_TYPE_STRING_LIST = 0x13


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [
        ("fmtid", _GUID),
        ("prop_id", ctypes.c_ulong),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", _GUID),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.c_void_p),
    ]


# Windows fills this for a device whose driver gives no friendly name. It holds
# the product string the USB device reports about itself, which is the only
# human-readable name available for most peripherals.
_BUS_REPORTED_DESC = _DEVPROPKEY(
    _GUID(
        0x540B947E,
        0x8B40,
        0x45BC,
        (ctypes.c_ubyte * 8)(0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2),
    ),
    4,
)

# Class names Windows uses when a device reports nothing about itself.
_GENERIC_DEVICE_NAMES = frozenset(
    name.casefold()
    for name in (
        "USB Composite Device",
        "USB Input Device",
        "USB Device",
        "Generic USB Hub",
        "Generic SuperSpeed USB Hub",
        "USB Root Hub",
        "USB Host Controller",
        "Unknown USB Device",
        "HID-compliant device",
        "HID-compliant vendor-defined device",
        "HID-compliant mouse",
        "HID-compliant system controller",
        "HID-compliant consumer control device",
        "USB Video Device",
        "USB Audio Device",
        "USB Serial Device",
        "WinUsb Device",
    )
)


def useful_device_name(name: str) -> bool:
    """True when a name says something about the device, not just its USB class."""
    cleaned = name.strip()
    return bool(cleaned) and cleaned.casefold() not in _GENERIC_DEVICE_NAMES


def parse_windows_devices(output: str) -> List[Tuple[str, str]]:
    """(vid:pid, name) pairs from the PowerShell fallback's output."""
    found: List[Tuple[str, str]] = []
    seen = set()
    for line in output.splitlines():
        # PowerShell emits a real tab only from a double-quoted format string.
        # Older builds passed a single-quoted one, which leaves backtick-t literal.
        separator = "\t" if "\t" in line else "`t"
        if separator not in line:
            continue
        vidpid, name = line.split(separator, 1)
        vidpid = vidpid.strip().lower()
        if not _VIDPID_RE.fullmatch(vidpid) or vidpid in seen:
            continue
        seen.add(vidpid)
        found.append((vidpid, name.strip() or vidpid))
    return found


def windows_device_script() -> str:
    """PowerShell fallback. The format string must stay double-quoted: PowerShell
    only expands `t inside double quotes, and the parser needs a real tab.

    BusReportedDeviceDesc carries the product string a USB device reports about
    itself, and child interfaces carry it when the parent does not.
    """
    return (
        "$names = @{}; "
        "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\\\VID_' } | "
        "ForEach-Object { if ($_.InstanceId -match 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})') { "
        "$id = '{0}:{1}' -f $Matches[1].ToLower(), $Matches[2].ToLower(); "
        "$name = $null; "
        "foreach ($key in 'DEVPKEY_Device_BusReportedDeviceDesc','DEVPKEY_Device_FriendlyName') { "
        "try { $name = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName $key -ErrorAction Stop).Data } catch { }; "
        "if ($name) { break } }; "
        "if (-not $name) { $name = $_.FriendlyName }; "
        "if ($name -and (-not $names[$id] -or $name.Length -gt $names[$id].Length)) { $names[$id] = $name } } }; "
        "$names.GetEnumerator() | ForEach-Object { \"{0}`t{1}\" -f $_.Key, $_.Value }"
    )


def _registry_property(setupapi, devinfo, data, prop: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    needed = ctypes.c_ulong()
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        devinfo, ctypes.byref(data), prop, None, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
    )
    if not ok:
        return ""
    # The buffer holds a REG_MULTI_SZ for hardware ids; the first entry is enough.
    return buffer.value.split("\x00", 1)[0] if buffer.value else ""


def _device_property(setupapi, devinfo, data, key) -> str:
    """A string device property, or "" when the device does not expose one."""
    buffer = ctypes.create_unicode_buffer(512)
    needed = ctypes.c_ulong()
    prop_type = ctypes.c_ulong()
    ok = setupapi.SetupDiGetDevicePropertyW(
        devinfo,
        ctypes.byref(data),
        ctypes.byref(key),
        ctypes.byref(prop_type),
        buffer,
        ctypes.sizeof(buffer),
        ctypes.byref(needed),
        0,
    )
    if not ok or prop_type.value not in (_DEVPROP_TYPE_STRING, _DEVPROP_TYPE_STRING_LIST):
        return ""
    return buffer.value.split("\x00", 1)[0].strip() if buffer.value else ""


def devices_via_setupapi() -> Optional[List[Tuple[str, str]]]:
    """Present USB devices read in-process, so one poll costs microseconds.

    None means SetupAPI was unusable and the caller should fall back to PowerShell.
    """
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    except (AttributeError, OSError):
        return None
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong
    ]
    setupapi.SetupDiEnumDeviceInfo.restype = ctypes.c_bool
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_SP_DEVINFO_DATA)
    ]
    setupapi.SetupDiGetDeviceRegistryPropertyW.restype = ctypes.c_bool
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVINFO_DATA),
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_bool
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiGetDevicePropertyW.restype = ctypes.c_bool
    setupapi.SetupDiGetDevicePropertyW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVINFO_DATA),
        ctypes.POINTER(_DEVPROPKEY),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
    ]

    devinfo = setupapi.SetupDiGetClassDevsW(
        None, None, None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES
    )
    if not devinfo or devinfo == ctypes.c_void_p(-1).value:
        return None

    # A composite device reports its product string on its child interfaces, and
    # the parent usually has no name at all, so names are collected per VID:PID
    # from every instance and the most descriptive one wins.
    names: Dict[str, str] = {}
    order: List[str] = []
    try:
        index = 0
        while True:
            data = _SP_DEVINFO_DATA()
            data.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
            if not setupapi.SetupDiEnumDeviceInfo(devinfo, index, ctypes.byref(data)):
                break
            index += 1
            hardware = _registry_property(setupapi, devinfo, data, _SPDRP_HARDWAREID)
            # Same scope as the PowerShell query, but child interfaces are kept:
            # they are where the product string lives.
            if not hardware.upper().startswith("USB\\VID_"):
                continue
            match = _WINDOWS_HWID_RE.search(hardware)
            if not match:
                continue
            vidpid = "{}:{}".format(match.group(1), match.group(2)).lower()
            if vidpid not in names:
                order.append(vidpid)
                names[vidpid] = ""

            candidates = (
                _device_property(setupapi, devinfo, data, _BUS_REPORTED_DESC),
                _registry_property(setupapi, devinfo, data, _SPDRP_FRIENDLYNAME),
                _registry_property(setupapi, devinfo, data, _SPDRP_DEVICEDESC),
            )
            current = names[vidpid]
            for candidate in candidates:
                if not useful_device_name(candidate):
                    continue
                if not current or len(candidate) > len(current):
                    current = candidate
            names[vidpid] = current
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devinfo)
    return [(vidpid, names[vidpid] or vidpid) for vidpid in order]


def connected_devices() -> List[Tuple[str, str]]:
    """Present USB devices, via SetupAPI when possible and PowerShell otherwise."""
    try:
        direct = devices_via_setupapi()
    except (OSError, AttributeError):
        direct = None
    if direct is not None:
        return direct
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", windows_device_script()],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_windows_devices(output)
