"""Monitor discovery and input switching, one module per platform.

`discover_displays` and `switch_monitor` are the single dispatch points. The
value-to-argv helpers and the monitor-name rule live here because every
platform shares them; only the transport is per-OS.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from kvm_switcher import paths
from kvm_switcher.config import load_config, parse_input_value

VCP_INPUT = 0x60


@dataclass
class FoundDisplay:
    name: str
    bus: str = ""
    tag: str = ""
    inputs: List[Tuple[str, str]] = field(default_factory=list)


def monitor_matches(description: str, wanted: str) -> bool:
    """True when a saved monitor name is a case-insensitive substring."""
    if not wanted:
        return True
    return wanted.casefold() in description.casefold()


def vcp_text(text: str) -> str:
    """Value string passed to ddcutil / BetterDisplay. Hex stays hex; decimals become 0x."""
    stripped = text.strip()
    value = parse_input_value(stripped)
    return stripped if stripped.lower().startswith("0x") else hex(value)


def discover_displays() -> List[FoundDisplay]:
    """Every monitor that reports its input list, or [] when none can be asked."""
    key = paths.platform_key()
    if key == "windows":
        from kvm_switcher.displays import windows

        try:
            return windows.discover()
        except (OSError, AttributeError):
            return []
    if key != "linux":
        return []
    from kvm_switcher.displays import linux

    return linux.discover()


def switch_monitor(input_text: Optional[str] = None) -> int:
    """Switch the configured monitor once and return the command's exit code."""
    display = load_config(paths.config_path()).display
    configured = display.input if display and display.input else None
    text = input_text or configured
    if not text:
        raise SystemExit("No monitor input configured. Run: kvm-switcher configure")
    value = parse_input_value(text)
    bus = display.bus if display else ""
    tag = display.tag if display else ""
    name = display.name if display else ""
    key = paths.platform_key()
    if key == "darwin":
        if not tag:
            raise SystemExit("No BetterDisplay tag configured. Run: kvm-switcher configure")
        from kvm_switcher.displays import darwin

        result = subprocess.run(darwin.switch_argv(text, tag, darwin.binary()))
        return result.returncode
    if key == "linux":
        from kvm_switcher.displays import linux

        result = subprocess.run(linux.switch_argv(text, bus))
        return result.returncode
    if key == "windows":
        from kvm_switcher.displays import windows

        changed = windows.switch(value, name)
        if changed < 1:
            print("No monitor accepted input select.", file=sys.stderr)
            return 1
        print("Switched {} monitor(s) to {}".format(changed, text))
        return 0
    print("No display switch for {}".format(key), file=sys.stderr)
    return 1
