#!/usr/bin/env python3
"""Watch USB devices and switch the monitor input when one of them appears.

One program for Linux, macOS, and Windows. Settings live in
~/.config/kvm-switcher/config.json (override with KVM_CONFIG).

  kvm-switcher.py                 watch (asks to install on first interactive run)
  kvm-switcher.py configure       pick devices, the monitor, and its input
  kvm-switcher.py status          show saved choices and whether the watcher is running
  kvm-switcher.py install         run at login
  kvm-switcher.py uninstall       stop it and remove the login entry
  kvm-switcher.py switch [code]  switch once, or use the saved input
  kvm-switcher.py check           report whether a newer version is published
  kvm-switcher.py update          replace this program with the published version
  kvm-switcher.py version         print the version
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.2.2"
INTERVAL = 0.5
VCP_INPUT = 0x60
DEV_RE = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\b\s*(?:#\s*(.*))?$")
INPUT_RE = re.compile(r"^input\s+(\S+)\s*$", re.I)
TAG_RE = re.compile(r"^tag\s+(\d+)\s*$", re.I)
_INPUT_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{2})\s*:\s*(.+)$")
_MCCS_INPUT_RE = re.compile(r"\b60\s*\(([^)]*)\)", re.I)


@dataclass
class DisplayTarget:
    input: str
    label: str = ""
    bus: str = ""
    tag: str = ""
    name: str = ""


@dataclass
class FoundDisplay:
    name: str
    bus: str = ""
    tag: str = ""
    inputs: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Config:
    devices: list[tuple[str, str]] = field(default_factory=list)
    display: DisplayTarget | None = None


def platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform == "win32":
        return "windows"
    return sys.platform


def config_path() -> Path:
    override = os.environ.get("KVM_CONFIG") or os.environ.get("KVM_DEVICES_FILE")
    if override:
        return Path(override)
    if platform_key() == "windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "kvm-switcher" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "kvm-switcher" / "config.json"


def seed_path() -> Path | None:
    candidate = Path(__file__).resolve().parent / "config.json"
    return candidate if candidate.is_file() else None


def legacy_config_path(path: Path) -> Path:
    return path.with_name("devices.txt")


def parse_legacy(path: Path) -> Config:
    devices: list[tuple[str, str]] = []
    input_value = None
    tag = None
    if not path.is_file():
        return Config()
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        dev = DEV_RE.match(stripped)
        if dev:
            vidpid = f"{dev.group(1)}:{dev.group(2)}".lower()
            name = (dev.group(3) or vidpid).strip()
            devices.append((vidpid, name))
            continue
        bare = stripped.split("#", 1)[0].strip()
        found = INPUT_RE.match(bare)
        if found:
            input_value = found.group(1)
            continue
        found = TAG_RE.match(bare)
        if found:
            tag = found.group(1)
    display = None
    if input_value or tag:
        display = DisplayTarget(input=input_value or "", tag=tag or "")
    return Config(devices, display)


def parse_json_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} must be a JSON object")
    devices: list[tuple[str, str]] = []
    for item in raw.get("devices") or []:
        if not isinstance(item, dict):
            continue
        vidpid = str(item.get("id", "")).strip().lower()
        if not _VIDPID_RE.fullmatch(vidpid):
            continue
        name = str(item.get("name") or vidpid).strip()
        devices.append((vidpid, name))
    display = None
    block = raw.get("display")
    if isinstance(block, dict):
        display = DisplayTarget(
            input=str(block.get("input") or ""),
            label=str(block.get("inputLabel") or ""),
            bus=str(block.get("bus") or ""),
            tag=str(block.get("tag") or ""),
            name=str(block.get("name") or ""),
        )
    return Config(devices, display)


def load_config(path: Path) -> Config:
    if path.suffix.lower() == ".txt":
        return parse_legacy(path)
    if path.is_file():
        return parse_json_config(path)
    legacy = legacy_config_path(path)
    if legacy.is_file():
        return parse_legacy(legacy)
    return Config()


def effective_input(explicit: str | None) -> str | None:
    return explicit or None


def parse_input_value(text: str) -> int:
    text = text.strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text, 10)


def load_saved(path: Path) -> Config:
    loaded = load_config(path)
    exists = path.is_file() or (path.suffix.lower() != ".txt" and legacy_config_path(path).is_file())
    if loaded.devices or exists:
        return loaded
    seed = seed_path()
    if seed and seed != path:
        return load_config(seed)
    return loaded


def connected_devices() -> list[tuple[str, str]]:
    key = platform_key()
    if key == "linux":
        return _connected_lsusb()
    if key == "darwin":
        return _connected_ioreg()
    if key == "windows":
        return _connected_windows()
    return []


_LSUSB_RE = re.compile(r"ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4}) (.*)")
_VIDPID_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
# Linux USB root hubs. They are always present and never a KVM trigger.
_LINUX_ROOT_HUB = "1d6b:"


def parse_lsusb(output: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        match = _LSUSB_RE.search(line)
        if not match:
            continue
        vidpid = f"{match.group(1)}:{match.group(2)}".lower()
        if vidpid.startswith(_LINUX_ROOT_HUB) or vidpid in seen:
            continue
        seen.add(vidpid)
        found.append((vidpid, match.group(3).strip()))
    return found


def _connected_lsusb() -> list[tuple[str, str]]:
    try:
        output = subprocess.check_output(["lsusb"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_lsusb(output)


def parse_ioreg(output: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    name = ""
    vendor = ""
    product = ""

    def flush() -> None:
        nonlocal vendor, product
        if not vendor or not product:
            vendor = product = ""
            return
        try:
            vidpid = f"{int(vendor, 16):04x}:{int(product, 16):04x}"
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


def _connected_ioreg() -> list[tuple[str, str]]:
    try:
        output = subprocess.check_output(
            ["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w0", "-x"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return parse_ioreg(output)


def parse_windows_devices(output: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
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


_DIGCF_PRESENT = 0x02
_DIGCF_ALLCLASSES = 0x04
_SPDRP_DEVICEDESC = 0x00
_SPDRP_HARDWAREID = 0x01
_SPDRP_SERVICE = 0x04
_SPDRP_FRIENDLYNAME = 0x0C
_WINDOWS_HWID_RE = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)
_DEVPROP_TYPE_STRING = 0x12
_DEVPROP_TYPE_STRING_LIST = 0x13

# Windows fills this for a device whose driver gives no friendly name. It holds
# the product string the USB device reports about itself, which is the only
# human-readable name available for most peripherals.
_BUS_REPORTED_DESC = _DEVPROPKEY(
    _GUID(0x540B947E, 0x8B40, 0x45BC, (ctypes.c_ubyte * 8)(0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2)),
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


def _device_property(setupapi, devinfo, data, key: _DEVPROPKEY) -> str:
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


def _windows_devices_setupapi() -> list[tuple[str, str]] | None:
    """Present USB devices read in-process, so one poll costs microseconds.

    None means SetupAPI was unusable and the caller should fall back to PowerShell.
    """
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    except OSError:
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
    names: dict[str, str] = {}
    order: list[str] = []
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
            vidpid = f"{match.group(1)}:{match.group(2)}".lower()
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


def _connected_windows() -> list[tuple[str, str]]:
    if platform_key() == "windows":
        try:
            direct = _windows_devices_setupapi()
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


def catalog(saved: list[tuple[str, str]]) -> list[tuple[str, str]]:
    items = list(connected_devices())
    present = {vidpid for vidpid, _ in items}
    for vidpid, name in saved:
        if vidpid in present:
            continue
        label = name if name.endswith("(not connected)") else f"{name} (not connected)"
        items.append((vidpid, label))
    return items


def parse_ddcutil_detect(output: str) -> list[FoundDisplay]:
    displays: list[FoundDisplay] = []
    invalid = False
    bus = ""
    name = ""

    def flush() -> None:
        nonlocal bus, name, invalid
        if bus and not invalid:
            displays.append(FoundDisplay(name=name or f"I2C bus {bus}", bus=bus))
        bus = ""
        name = ""
        invalid = False

    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("Display ") or line.startswith("Invalid display"):
            flush()
            invalid = line.startswith("Invalid display")
            continue
        bus_match = re.search(r"I2C bus:\s+/dev/i2c-(\d+)", line)
        if bus_match:
            bus = bus_match.group(1)
            continue
        model = re.match(r"Model:\s*(.+)", line)
        if model:
            name = model.group(1).strip()
    flush()
    return displays


_MCCS_INPUT_NAMES = {
    "01": "Analog-1",
    "02": "Analog-2",
    "03": "Digital-1",
    "04": "Digital-2",
    "05": "Composite-1",
    "06": "Composite-2",
    "07": "SVideo-1",
    "08": "SVideo-2",
    "09": "Tuner-1",
    "0a": "Tuner-2",
    "0b": "Tuner-3",
    "0c": "Component-1",
    "0d": "Component-2",
    "0e": "Component-3",
    "0f": "DisplayPort-1",
    "10": "DisplayPort-2",
    "11": "HDMI-1",
    "12": "HDMI-2",
    "13": "HDMI-3",
    "14": "HDMI-4",
    "15": "DVI-1",
    "16": "DVI-2",
    "17": "Thunderbolt-1",
    "18": "Thunderbolt-2",
}


def input_label(code: str) -> str:
    """Human name for an MCCS input-select value, or "" when it is not a known one."""
    return _MCCS_INPUT_NAMES.get(code.strip().lower().removeprefix("0x"), "")


def parse_input_sources(output: str) -> list[tuple[str, str]]:
    """Input-select values from `ddcutil capabilities` or an MCCS capability string."""
    sources: list[tuple[str, str]] = []
    in_feature = False
    in_values = False
    for raw in output.splitlines():
        line = raw.strip()
        if re.search(r"Feature:\s*60\b", line):
            in_feature = True
            in_values = False
            continue
        if in_feature and line.startswith("Feature:"):
            break
        if in_feature and line.startswith("Values:"):
            in_values = True
            continue
        if not in_values:
            continue
        match = _INPUT_LINE_RE.match(raw)
        if match:
            sources.append((f"0x{match.group(1).lower()}", match.group(2).strip()))
            continue
        if line:
            break
    if sources:
        return sources
    return parse_mccs_inputs(output)


def parse_mccs_inputs(output: str) -> list[tuple[str, str]]:
    """Input values from an MCCS capability string such as `vcp(... 60(11 12 0F 10) ...)`."""
    mccs = _MCCS_INPUT_RE.search(output)
    if not mccs:
        return []
    sources: list[tuple[str, str]] = []
    for token in mccs.group(1).split():
        if not re.fullmatch(r"[0-9a-fA-F]{2}", token):
            continue
        code = token.lower()
        sources.append((f"0x{code}", input_label(code)))
    return sources


def _command_output(command: list[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return ""


def discover_displays() -> list[FoundDisplay]:
    key = platform_key()
    if key == "windows":
        try:
            return _windows_displays()
        except (OSError, AttributeError):
            return []
    if key != "linux":
        return []
    found = parse_ddcutil_detect(_command_output(["ddcutil", "detect"]))
    for display in found:
        caps = _command_output(["ddcutil", "--bus", display.bus, "capabilities"])
        display.inputs = parse_input_sources(caps)
    return found


def monitor_matches(description: str, wanted: str) -> bool:
    if not wanted:
        return True
    return wanted.casefold() in description.casefold()


def vcp_text(text: str) -> str:
    """Value string passed to ddcutil / BetterDisplay. Hex stays hex; decimals become 0x."""
    stripped = text.strip()
    value = parse_input_value(stripped)
    return stripped if stripped.lower().startswith("0x") else hex(value)


def linux_switch_argv(text: str, bus: str = "") -> list[str]:
    command = ["ddcutil"]
    if bus:
        command.extend(["--bus", bus])
    command.extend(["setvcp", "60", vcp_text(text)])
    return command


def darwin_switch_argv(text: str, tag: str, binary: str) -> list[str]:
    return [
        binary, "set",
        f"--tagID={tag}",
        "--feature=ddc",
        "--vcp=inputSelect",
        f"--value={vcp_text(text)}",
    ]


def save_config(path: Path, chosen: list[str], items: list[tuple[str, str]], display: DisplayTarget) -> None:
    if path.suffix.lower() == ".txt":
        path = path.with_name("config.json")
    names = {vidpid: name for vidpid, name in items}
    payload: dict = {
        "devices": [
            {"id": vidpid, "name": names.get(vidpid, vidpid).removesuffix(" (not connected)")}
            for vidpid in chosen
        ],
        "display": {
            "name": display.name,
            "bus": display.bus,
            "tag": display.tag,
            "input": display.input,
            "inputLabel": display.label,
        },
    }
    payload["display"] = {key: value for key, value in payload["display"].items() if value}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def prompt_manual_display(current: str, saved: DisplayTarget | None) -> DisplayTarget:
    while True:
        hint = f" [{current}]" if current else ""
        typed = input(f"Monitor input{hint}: ").strip()
        input_value = typed or current
        if not input_value:
            print("Enter the monitor input code, such as 0x0f or 15.")
            continue
        try:
            parse_input_value(input_value)
        except ValueError:
            print("Enter a number such as 0x0f or 15.")
            continue
        break
    tag = (saved.tag if saved and saved.tag else "")
    if platform_key() == "darwin":
        while True:
            hint = f" [{tag}]" if tag else ""
            typed_tag = input(f"BetterDisplay tag{hint}: ").strip()
            tag = typed_tag or tag
            if tag.isdigit():
                break
            print("Enter the BetterDisplay tag id.")
    return DisplayTarget(
        input=vcp_text(input_value),
        bus=saved.bus if saved else "",
        tag=tag,
        name=saved.name if saved else "",
        label=saved.label if saved and saved.input == vcp_text(input_value) else "",
    )


def prompt_display(displays: list[FoundDisplay], saved: DisplayTarget | None) -> DisplayTarget:
    current = (saved.input if saved and saved.input else "")
    if not displays:
        print("\nNo monitor reported its input list. Type the input code.")
        return prompt_manual_display(current, saved)
    chosen = displays[0]
    if len(displays) > 1:
        print("\nWhich monitor should switch?\n")
        for index, display in enumerate(displays, start=1):
            where = f"bus {display.bus}" if display.bus else display.tag
            print(f"  {index:2}  {display.name}  {where}".rstrip())
        print()
        while True:
            answer = input("Monitor: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(displays):
                chosen = displays[int(answer) - 1]
                break
            print(f"Enter a number from 1 to {len(displays)}.")
    if not chosen.inputs:
        target = prompt_manual_display(current, saved)
        target.bus = chosen.bus or target.bus
        target.tag = chosen.tag or target.tag
        target.name = chosen.name or target.name
        return target
    print(f"\nInput on {chosen.name}:\n")
    for index, (value, label) in enumerate(chosen.inputs, start=1):
        print(f"  {index:2}  {value}  {label}".rstrip())
    print()
    while True:
        answer = input("Input (number, or a code such as 0x0f): ").strip()
        if not answer:
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(chosen.inputs):
            value, label = chosen.inputs[int(answer) - 1]
            return DisplayTarget(input=value, label=label, bus=chosen.bus, tag=chosen.tag, name=chosen.name)
        try:
            parse_input_value(answer)
        except ValueError:
            print(f"Enter a number from 1 to {len(chosen.inputs)}, or a code such as 0x0f.")
            continue
        return DisplayTarget(input=vcp_text(answer), bus=chosen.bus, tag=chosen.tag, name=chosen.name)


def configure() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Device selection needs a terminal. Run: kvm-switcher.py configure")
    path = config_path()
    saved_config = load_saved(path)
    saved = saved_config.devices
    items = catalog(saved)
    if not items:
        raise SystemExit("No USB devices found.")
    selected = {vidpid for vidpid, _ in saved}
    while True:
        print("\nNumbers toggle devices. a selects all, n selects none.")
        print("s saves, q cancels. Any selected device switches the monitor.\n")
        for index, (vidpid, name) in enumerate(items, start=1):
            mark = "[x]" if vidpid in selected else "[ ]"
            print(f"  {mark} {index:2}  {vidpid}  {name}")
        print()
        answer = input("Choice: ").strip()
        if answer.lower() == "s":
            break
        if answer.lower() == "q":
            raise SystemExit("Device selection cancelled.")
        if answer.lower() == "a":
            selected = {vidpid for vidpid, _ in items}
            continue
        if answer.lower() == "n":
            selected = set()
            continue
        for token in answer.replace(",", " ").split():
            if not token.isdigit():
                continue
            number = int(token)
            if number < 1 or number > len(items):
                continue
            vidpid = items[number - 1][0]
            if vidpid in selected:
                selected.remove(vidpid)
            else:
                selected.add(vidpid)
    chosen = [vidpid for vidpid, _ in items if vidpid in selected]
    display = prompt_display(discover_displays(), saved_config.display)
    destination = path if path.suffix.lower() != ".txt" else path.with_name("config.json")
    save_config(destination, chosen, items, display)
    print(f"Saved {len(chosen)} device(s) to {destination}")
    for vidpid in chosen:
        print(f"  {vidpid}")
    label = f" {display.label}" if display.label else ""
    where = display.name or (f"bus {display.bus}" if display.bus else "")
    print(f"  input {display.input}{label}" + (f" on {where}" if where else ""))


def ensure_config() -> None:
    path = config_path()
    if load_config(path).devices:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        configure()
        return
    seed = seed_path()
    if seed and not path.is_file() and not legacy_config_path(path).is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")


def devices_present(wanted: list[str]) -> bool:
    if not wanted:
        return False
    present = {vidpid for vidpid, _ in connected_devices()}
    return any(vidpid in present for vidpid in wanted)


def switch_monitor(input_text: str | None = None) -> int:
    display = load_config(config_path()).display
    configured = display.input if display and display.input else None
    text = input_text or configured
    if not text:
        raise SystemExit("No monitor input configured. Run: kvm-switcher.py configure")
    value = parse_input_value(text)
    bus = display.bus if display else ""
    tag = display.tag if display else ""
    name = display.name if display else ""
    if platform_key() == "darwin" and not tag:
        raise SystemExit("No BetterDisplay tag configured. Run: kvm-switcher.py configure")
    key = platform_key()
    if key == "linux":
        result = subprocess.run(linux_switch_argv(text, bus))
        return result.returncode
    if key == "darwin":
        binary = shutil.which("betterdisplaycli") or "/opt/homebrew/bin/betterdisplaycli"
        result = subprocess.run(darwin_switch_argv(text, tag, binary))
        return result.returncode
    if key == "windows":
        changed = _switch_windows(value, name)
        if changed < 1:
            print("No monitor accepted input select.", file=sys.stderr)
            return 1
        print(f"Switched {changed} monitor(s) to {text}")
        return 0
    print(f"No display switch for {key}", file=sys.stderr)
    return 1


# Windows reports many panels as a placeholder instead of the real model name,
# so a monitor friendly name saved from another OS can never match.
_WINDOWS_GENERIC_DESCRIPTIONS = frozenset({"generic pnp monitor", "generic monitor", "pnp monitor"})


def windows_monitor_matches(description: str, wanted: str) -> bool:
    if monitor_matches(description, wanted):
        return True
    return description.strip().casefold() in _WINDOWS_GENERIC_DESCRIPTIONS


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


class _PhysicalHandles:
    """Physical monitor handles that stay valid until the block exits."""

    def __init__(self, dxva2, monitors):
        self._dxva2 = dxva2
        self.monitors = monitors

    def __enter__(self):
        return self.monitors

    def __exit__(self, *_exc):
        self._dxva2.DestroyPhysicalMonitors(len(self.monitors), self.monitors)
        return False


def _windows_physical_monitors():
    """(dxva2, [(description, handle)]) for every physical monitor Windows reports."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
    collected: list[tuple[str, ctypes.c_void_p, object, ctypes.c_uint]] = []

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
    destroyed: set[int] = set()
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


def _windows_displays() -> list[FoundDisplay]:
    """Windows has no ddcutil, so ask each monitor for its own input list."""
    dxva2, collected = _windows_physical_monitors()
    displays: list[FoundDisplay] = []
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


def watch() -> None:
    ensure_config()
    maybe_install()
    # Two watchers would both switch on the same detection, which looks like a
    # double flash. Refuse to start when one is already running this install.
    existing = read_watcher_pid()
    if existing and existing != os.getpid() and process_alive(existing):
        raise SystemExit(
            f"A watcher is already running (pid {existing}). "
            "Stop it first: kvm-switcher.py uninstall"
        )
    write_watcher_pid(os.getpid())
    try:
        _watch_loop()
    finally:
        # Only clear the record if it still identifies this exact process.
        if read_watcher_pid() == os.getpid():
            recorded = _read_watcher_start()
            if not recorded or _pid_start_time(os.getpid()) == recorded:
                try:
                    watcher_pid_path().unlink()
                except OSError:
                    pass


def _watch_loop() -> None:
    active = False
    while True:
        wanted = [vidpid for vidpid, _ in load_config(config_path()).devices]
        try:
            detected = devices_present(wanted)
        except (OSError, subprocess.CalledProcessError):
            detected = False
        if detected:
            if not active:
                # Set before switching: a failed switch must not leave the flag
                # False, or every poll would retry and flash the screen again.
                active = True
                print("USB device detected — switching display")
                try:
                    switch_monitor()
                except (OSError, subprocess.SubprocessError) as exc:
                    print(f"Switch failed: {exc}", file=sys.stderr)
        else:
            active = False
        time.sleep(INTERVAL)


def format_status(config: Config, installed: bool, running: bool) -> str:
    lines: list[str] = []
    display = config.display
    configured = bool(config.devices) or bool(display and display.input)
    if not configured:
        lines.append("Not configured. Run: kvm-switcher.py configure")
    else:
        lines.append("Devices:")
        if config.devices:
            for vidpid, name in config.devices:
                lines.append(f"  {vidpid}  {name}")
        else:
            lines.append("  (none)")
        if display and display.input:
            label = f" {display.label}" if display.label else ""
            where = []
            if display.name:
                where.append(display.name)
            if display.bus:
                where.append(f"bus {display.bus}")
            if display.tag:
                where.append(f"tag {display.tag}")
            suffix = f" on {', '.join(where)}" if where else ""
            lines.append(f"Display: {display.input}{label}{suffix}")
        else:
            lines.append("Display: not chosen")
    if running:
        lines.append("Watcher: running")
    elif installed:
        lines.append("Watcher: installed, not running")
    else:
        lines.append("Watcher: not installed")
    return "\n".join(lines)


def parse_systemctl_is_active(text: str) -> bool:
    return text.strip() == "active"


def parse_launchctl_list(text: str) -> bool:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or "kvm-switcher" not in parts[-1]:
            continue
        return parts[0].isdigit()
    return False


def parse_schtasks_status(text: str) -> bool:
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "status":
            return value.strip().lower() == "running"
    return False


def watcher_pid_path() -> Path:
    return config_path().with_name("watcher.pid")


def _pid_start_time(pid: int) -> str:
    """Creation time of a live process, so a recycled pid is not mistaken for ours."""
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            ok = kernel32.GetProcessTimes(
                ctypes.c_void_p(handle),
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            return str(creation.value) if ok else ""
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat.split(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def write_watcher_pid(pid: int) -> None:
    start = _pid_start_time(pid)
    try:
        path = watcher_pid_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}\n{start}\n", encoding="utf-8")
    except OSError:
        pass


def read_watcher_pid() -> int | None:
    try:
        first = watcher_pid_path().read_text(encoding="utf-8").strip().splitlines()[0]
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


def _read_watcher_start() -> str:
    try:
        lines = watcher_pid_path().read_text(encoding="utf-8").strip().splitlines()
        return lines[1].strip() if len(lines) > 1 else ""
    except OSError:
        return ""


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_watcher_process() -> bool:
    """Stop a watcher this user started, but never an unrelated process that
    happens to have inherited its pid after the watcher exited."""
    pid = read_watcher_pid()
    recorded = _read_watcher_start()
    stopped = False
    try:
        if pid and process_alive(pid) and (not recorded or _pid_start_time(pid) == recorded):
            if sys.platform == "win32":
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                PROCESS_TERMINATE = 0x0001
                kernel32.OpenProcess.restype = ctypes.c_void_p
                handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if handle:
                    try:
                        stopped = bool(kernel32.TerminateProcess(ctypes.c_void_p(handle), 1))
                    finally:
                        kernel32.CloseHandle(ctypes.c_void_p(handle))
            else:
                try:
                    os.kill(pid, 15)
                    stopped = True
                except OSError:
                    stopped = False
    finally:
        try:
            watcher_pid_path().unlink()
        except OSError:
            pass
    return stopped


def service_running() -> bool:
    key = platform_key()
    if key == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "kvm-switcher.service"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return parse_systemctl_is_active(result.stdout)
    if key == "darwin":
        result = subprocess.run(
            ["launchctl", "list", "com.kvm-switcher.watcher"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return parse_launchctl_list(result.stdout)
    if key == "windows":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "kvm-switcher", "/FO", "LIST"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if parse_schtasks_status(result.stdout):
            return True
        pid = read_watcher_pid()
        return bool(pid and process_alive(pid))
    return False


def status() -> None:
    print(format_status(load_config(config_path()), service_installed(), service_running()))


def service_installed() -> bool:
    key = platform_key()
    if key == "linux":
        return (Path.home() / ".config/systemd/user/kvm-switcher.service").is_file()
    if key == "darwin":
        return (Path.home() / "Library/LaunchAgents/com.kvm-switcher.watcher.plist").is_file()
    if key == "windows":
        task = subprocess.run(
            ["schtasks", "/Query", "/TN", "kvm-switcher"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if task.returncode == 0:
            return True
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as run_key:
                winreg.QueryValueEx(run_key, "kvm-switcher")
            return True
        except OSError:
            return False
    return False


def maybe_install() -> None:
    if service_installed():
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    answer = input("Install the watcher to run at login? [y/N] ").strip()
    if answer.lower().startswith("y"):
        install()
        raise SystemExit(0)
    print("Skipping installation. The watcher will run only in this window.")


def install() -> None:
    key = platform_key()
    bindir = Path.home() / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / "kvm-switcher.py"
    shutil.copy2(Path(__file__).resolve(), target)
    seed = seed_path()
    if seed:
        shutil.copy2(seed, bindir / "config.json")
    python = sys.executable
    if key == "linux":
        unit_dir = Path.home() / ".config/systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "kvm-switcher.service"
        unit.write_text(
            "\n".join([
                "[Unit]",
                "Description=KVM Switcher",
                "After=graphical.target",
                "",
                "[Service]",
                f"ExecStart={python} {target}",
                "Restart=always",
                "RestartSec=1",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]),
            encoding="utf-8",
        )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "kvm-switcher.service"], check=False)
    elif key == "darwin":
        agents = Path.home() / "Library/LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / "com.kvm-switcher.watcher.plist"
        plist.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kvm-switcher.watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{target}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
""",
            encoding="utf-8",
        )
        subprocess.run(["launchctl", "load", str(plist)], check=False)
    elif key == "windows":
        command = f'"{python}" "{target}"'
        created = subprocess.run(
            ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", "kvm-switcher", "/TR", command],
        )
        if created.returncode != 0:
            # A Scheduled Task needs elevation on some machines. The per-user
            # Run key does not, so it is the fallback rather than a failure.
            print(
                "Scheduled Task could not be created "
                f"(schtasks exit {created.returncode}); using the per-user Run key instead.",
                file=sys.stderr,
            )
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as run_key:
                winreg.SetValueEx(run_key, "kvm-switcher", 0, winreg.REG_SZ, command)
        child = subprocess.Popen([python, str(target)], close_fds=True)
        write_watcher_pid(child.pid)
    else:
        raise SystemExit(f"No service install for {key}")
    print("Installation complete. The watcher is running in the background.")
    print("Change the device pool with: kvm-switcher.py configure")
    print("Remove it with: kvm-switcher.py uninstall")


def uninstall() -> None:
    """Stop the login watcher and delete its service entry. The device list stays."""
    key = platform_key()
    removed = False
    if key == "linux":
        unit = Path.home() / ".config/systemd/user/kvm-switcher.service"
        subprocess.run(["systemctl", "--user", "disable", "--now", "kvm-switcher.service"], check=False)
        if unit.is_file():
            unit.unlink()
            removed = True
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    elif key == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.kvm-switcher.watcher.plist"
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist)], check=False)
        subprocess.run(["launchctl", "unload", str(plist)], check=False)
        if plist.is_file():
            plist.unlink()
            removed = True
    elif key == "windows":
        # schtasks may not own the entry at all (the Run-key fallback does), so
        # report success only for something that really changed.
        stopped = stop_watcher_process()
        ended = subprocess.run(["schtasks", "/End", "/TN", "kvm-switcher"], check=False)
        deleted = subprocess.run(["schtasks", "/Delete", "/F", "/TN", "kvm-switcher"], check=False)
        if ended.returncode == 0 or deleted.returncode == 0:
            removed = True
        if stopped:
            removed = True
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as run_key:
                winreg.DeleteValue(run_key, "kvm-switcher")
            removed = True
        except OSError:
            pass
    else:
        raise SystemExit(f"No service uninstall for {key}")

    installed_copy = Path.home() / "bin" / "kvm-switcher.py"
    if installed_copy.is_file():
        installed_copy.unlink()
        removed = True
    if removed:
        print("Watcher removed. It will not start at login.")
    else:
        print("No watcher was installed.")
    print(f"Config kept at {config_path()}")


UPDATE_REPO = "algun/kvm-switcher"
# Same ref install.ps1 and install.sh fetch from. Keep the three in step.
UPDATE_REF = "main"
_VERSION_RE = re.compile(rb'^VERSION = "([^"]+)"', re.M)


def version_tuple(text: str) -> tuple[int, ...]:
    """Comparable form of a dotted version, tolerating a leading v and suffixes."""
    parts = text.strip().lstrip("vV").split(".")
    numbers: list[int] = []
    for part in parts:
        digits = re.match(r"\d+", part)
        if not digits:
            break
        numbers.append(int(digits.group(0)))
    return tuple(numbers)


def version_is_newer(candidate: str, current: str) -> bool:
    candidate_parts = version_tuple(candidate)
    current_parts = version_tuple(current)
    if not candidate_parts:
        return False
    # Pad so 0.2 compares as newer than 0.1.9 but not newer than 0.1.9.1
    width = max(len(candidate_parts), len(current_parts))
    pad = lambda parts: parts + (0,) * (width - len(parts))
    return pad(candidate_parts) > pad(current_parts)


def published_version(ref: str = UPDATE_REF, timeout: float = 15.0) -> str:
    """VERSION of the program published at ref. Raises SystemExit if unreachable."""
    url = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{ref}/kvm-switcher.py"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Could not reach {url}: {exc}") from exc
    match = _VERSION_RE.search(body)
    if not match:
        raise SystemExit(f"{url} does not look like kvm-switcher.py")
    return match.group(1).decode("utf-8", "replace")


def running_from_checkout() -> bool:
    """True when this file sits in a source checkout rather than an install dir."""
    return (Path(__file__).resolve().parent / ".git").is_dir()


def check() -> int:
    latest = published_version()
    if version_is_newer(latest, VERSION):
        print(f"Update available: {VERSION} -> {latest}")
        print("Run: kvm-switcher.py update")
        return 0
    if version_is_newer(VERSION, latest):
        print(f"Local version {VERSION} is newer than the published {latest}.")
        return 0
    print(f"Up to date ({VERSION}).")
    return 0


def _download_program(ref: str = UPDATE_REF) -> bytes:
    url = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{ref}/kvm-switcher.py"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"Could not download {url}: {exc}") from exc


def _verified_program(body: bytes, expected_newer_than: str) -> bytes:
    """The downloaded bytes, once they are known to be a working newer program."""
    # Checked first: a BOM would otherwise read as "not kvm-switcher.py", and it
    # breaks the shebang that install.sh symlinks onto PATH.
    if body.startswith(b"\xef\xbb\xbf"):
        raise SystemExit("Downloaded file starts with a BOM; nothing changed.")
    if not _VERSION_RE.search(body):
        raise SystemExit("Downloaded file is not kvm-switcher.py; nothing changed.")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Downloaded file is not valid UTF-8: {exc}") from exc
    # Prove it parses before it replaces a working install.
    try:
        compile(body, "<download>", "exec")
    except SyntaxError as exc:
        raise SystemExit(f"Downloaded file does not compile: {exc}") from exc
    match = _VERSION_RE.search(body)
    downloaded = match.group(1).decode("utf-8", "replace")
    if not version_is_newer(downloaded, expected_newer_than):
        raise SystemExit(
            f"Downloaded version {downloaded} is not newer than {expected_newer_than}; "
            "nothing changed."
        )
    return body


def update() -> int:
    """Replace this program with the published version, then restart the watcher."""
    target = Path(__file__).resolve()
    if running_from_checkout():
        raise SystemExit(
            f"This is a source checkout ({target.parent}). Update it with git instead: "
            "git pull"
        )
    latest = published_version()
    if not version_is_newer(latest, VERSION):
        print(f"Already up to date ({VERSION}).")
        return 0

    # Only touch the watcher when it is running this very file. A copy installed
    # elsewhere must never stop a watcher that belongs to a different install.
    watcher = running_watcher_target()
    owns_watcher = bool(watcher) and same_program(watcher, str(target))

    body = _verified_program(_download_program(), VERSION)

    stopped = False
    if owns_watcher:
        stopped = stop_watcher_process()

    # Write beside the target and swap, so a failure cannot leave a half file.
    replacement = target.with_name(target.name + ".new")
    try:
        replacement.write_bytes(body)
        shutil.copymode(target, replacement)
        os.replace(replacement, target)
    except OSError as exc:
        try:
            replacement.unlink()
        except OSError:
            pass
        raise SystemExit(f"Could not replace {target}: {exc}") from exc

    print(f"Updated {target} {VERSION} -> {latest}")

    if stopped:
        spawn_watcher(target)
        print("Watcher restarted.")
    elif watcher:
        print("A different watcher is running; restart it to use the new version.")
    return 0


def running_watcher_target() -> str:
    """Command target of the watcher this user is running, or "" when unknown."""
    pid = read_watcher_pid()
    if not pid or not process_alive(pid):
        return ""
    if sys.platform == "win32":
        return _windows_process_command(pid)
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    parts = [part for part in raw.decode("utf-8", "replace").split("\x00") if part]
    return " ".join(parts[1:])


def _windows_process_command(pid: int) -> str:
    """Command line of a process. Empty when it cannot be read."""
    if sys.platform != "win32":
        return ""
    # WMI is the only way without extra dependencies; keep it short so a slow
    # provider cannot stall an update.
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' "
                "-ErrorAction SilentlyContinue).CommandLine",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return output.strip()


def same_program(left: str, right: str) -> bool:
    """True when two command lines point at the same file."""
    if not left or not right:
        return False

    def normalise(text: str) -> str:
        text = text.strip().strip('"')
        try:
            return os.path.normcase(str(Path(text).resolve()))
        except OSError:
            return os.path.normcase(text)

    left_path = normalise(left)
    right_path = normalise(right)
    if left_path == right_path:
        return True
    # A command line holds the whole argv; compare its tail against the path.
    return left_path.endswith(right_path) or right_path.endswith(left_path)


def spawn_watcher(program: Path) -> int:
    """Start a detached watcher. Its stdio must not stay on the caller's pipes:
    an update run with captured output would otherwise wait on them forever."""
    process = subprocess.Popen(
        [sys.executable, str(program)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return process.pid


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "watch"
    if command in ("configure", "--configure", "-c"):
        configure()
    elif command in ("status", "--status"):
        status()
    elif command in ("install", "--install"):
        install()
    elif command in ("uninstall", "--uninstall", "remove", "--remove"):
        uninstall()
    elif command == "switch":
        value = argv[2] if len(argv) > 2 else None
        raise SystemExit(switch_monitor(value))
    elif command in ("watch",):
        watch()
    elif command in ("check", "--check"):
        raise SystemExit(check())
    elif command in ("update", "--update", "upgrade"):
        raise SystemExit(update())
    elif command in ("version", "--version", "-V"):
        print(VERSION)
    elif command in ("-h", "--help", "help"):
        print(__doc__)
    else:
        raise SystemExit(f"Unknown command {command!r}. Try: kvm-switcher.py --help")


if __name__ == "__main__":
    main(sys.argv)
