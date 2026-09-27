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
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.1.0"
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
        if "\t" not in line:
            continue
        vidpid, name = line.split("\t", 1)
        vidpid = vidpid.strip().lower()
        if not _VIDPID_RE.fullmatch(vidpid) or vidpid in seen:
            continue
        seen.add(vidpid)
        found.append((vidpid, name.strip() or vidpid))
    return found


def _connected_windows() -> list[tuple[str, str]]:
    script = (
        "Get-PnpDevice -PresentOnly | Where-Object { "
        "$_.InstanceId -match '^USB\\\\VID_' -and $_.InstanceId -notmatch '&MI_' } | "
        "ForEach-Object { if ($_.InstanceId -match 'VID_([0-9A-F]{4})&PID_([0-9A-F]{4})') { "
        "'{0}:{1}`t{2}' -f $Matches[1].ToLower(), $Matches[2].ToLower(), $_.FriendlyName } }"
    )
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
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
    mccs = _MCCS_INPUT_RE.search(output)
    if not mccs:
        return []
    for token in mccs.group(1).split():
        if re.fullmatch(r"[0-9a-fA-F]{2}", token):
            sources.append((f"0x{token.lower()}", ""))
    return sources


def _command_output(command: list[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return ""


def discover_displays() -> list[FoundDisplay]:
    if platform_key() != "linux":
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


def _switch_windows(value: int, name: str = "") -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class PHYSICAL_MONITOR(ctypes.Structure):
        _fields_ = [
            ("handle", ctypes.c_void_p),
            ("description", ctypes.c_wchar * 128),
        ]

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_ssize_t
    )
    switched = {"n": 0}

    def _callback(hmonitor, _hdc, _rect, _data):
        count = ctypes.c_uint()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmonitor, ctypes.byref(count)):
            return 1
        if count.value == 0:
            return 1
        monitors = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmonitor, count, monitors):
            return 1
        try:
            for monitor in monitors:
                if not monitor_matches(monitor.description, name):
                    continue
                if dxva2.SetVCPFeature(monitor.handle, VCP_INPUT, value):
                    switched["n"] += 1
        finally:
            dxva2.DestroyPhysicalMonitors(count, monitors)
        return 1

    callback = callback_type(_callback)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    return switched["n"]


def watch() -> None:
    ensure_config()
    maybe_install()
    active = False
    while True:
        wanted = [vidpid for vidpid, _ in load_config(config_path()).devices]
        try:
            detected = devices_present(wanted)
        except (OSError, subprocess.CalledProcessError):
            detected = False
        if detected:
            if not active:
                print("USB device detected — switching display")
                switch_monitor()
                active = True
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
        return parse_schtasks_status(result.stdout)
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
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as run_key:
                winreg.SetValueEx(run_key, "kvm-switcher", 0, winreg.REG_SZ, command)
        subprocess.Popen([python, str(target)], close_fds=True)
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
        ended = subprocess.run(["schtasks", "/End", "/TN", "kvm-switcher"], check=False)
        deleted = subprocess.run(["schtasks", "/Delete", "/F", "/TN", "kvm-switcher"], check=False)
        if ended.returncode == 0 or deleted.returncode == 0:
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
    elif command in ("version", "--version", "-V"):
        print(VERSION)
    elif command in ("-h", "--help", "help"):
        print(__doc__)
    else:
        raise SystemExit(f"Unknown command {command!r}. Try: kvm-switcher.py --help")


if __name__ == "__main__":
    main(sys.argv)
