"""Reading, seeding, and writing the JSON config file.

Settings live in ``~/.config/kvm-switcher/config.json`` (override with the
``KVM_CONFIG`` environment variable). Pure file handling, no OS branches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from kvm_switcher import install_root
from kvm_switcher.paths import config_destination, legacy_config_path

DEV_RE = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\b\s*(?:#\s*(.*))?$")
INPUT_RE = re.compile(r"^input\s+(\S+)\s*$", re.I)
TAG_RE = re.compile(r"^tag\s+(\d+)\s*$", re.I)
_VIDPID_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")


@dataclass
class DisplayTarget:
    input: str
    label: str = ""
    bus: str = ""
    tag: str = ""
    name: str = ""


@dataclass
class Config:
    devices: List[Tuple[str, str]] = field(default_factory=list)
    display: Optional[DisplayTarget] = None


def parse_legacy(path: Path) -> Config:
    """The old `devices.txt` format: vid:pid lines, plus `input` and `tag`."""
    devices: List[Tuple[str, str]] = []
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
            vidpid = "{}:{}".format(dev.group(1), dev.group(2)).lower()
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
    """The JSON config `configure` writes. Malformed input stops the program."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit("{} is not valid JSON: {}".format(path, exc)) from exc
    if not isinstance(raw, dict):
        raise SystemExit("{} must be a JSON object".format(path))
    devices: List[Tuple[str, str]] = []
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
    """Read the config, accepting a legacy `devices.txt` beside a missing JSON one."""
    if path.suffix.lower() == ".txt":
        return parse_legacy(path)
    if path.is_file():
        return parse_json_config(path)
    legacy = legacy_config_path(path)
    if legacy.is_file():
        return parse_legacy(legacy)
    return Config()


def load_saved(path: Path) -> Config:
    """The saved config, or the config shipped with the program when none is saved."""
    loaded = load_config(path)
    exists = path.is_file() or (path.suffix.lower() != ".txt" and legacy_config_path(path).is_file())
    if loaded.devices or exists:
        return loaded
    seed = install_root.seed_path()
    if seed and seed != path:
        return load_config(seed)
    return loaded


def effective_input(explicit: Optional[str]) -> Optional[str]:
    """An explicit input only, never a stored default."""
    return explicit or None


def parse_input_value(text: str) -> int:
    """An MCCS value from `0x0f` or `15`; raises ValueError on anything else."""
    text = text.strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text, 10)


def save_config(
    path: Path, chosen: List[str], items: List[Tuple[str, str]], display: DisplayTarget
) -> None:
    """Write the JSON config for the chosen devices and display."""
    path = config_destination(path)
    names = {vidpid: name for vidpid, name in items}
    payload = {
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
