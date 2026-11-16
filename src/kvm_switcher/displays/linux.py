"""Linux monitor discovery and switching through ``ddcutil``."""

from __future__ import annotations

import re
import subprocess
from typing import List

from kvm_switcher.displays import FoundDisplay, vcp_text
from kvm_switcher.mccs import parse_input_sources


def parse_ddcutil_detect(output: str) -> List[FoundDisplay]:
    """Displays from `ddcutil detect`, dropping ones it calls invalid."""
    displays: List[FoundDisplay] = []
    invalid = False
    bus = ""
    name = ""

    def flush() -> None:
        nonlocal bus, name, invalid
        if bus and not invalid:
            displays.append(FoundDisplay(name=name or "I2C bus " + bus, bus=bus))
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


def command_output(command: List[str]) -> str:
    """stdout of a command, or "" when it is missing or fails."""
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return ""


def discover() -> List[FoundDisplay]:
    """Each display ddcutil finds, with the input list it advertises."""
    found = parse_ddcutil_detect(command_output(["ddcutil", "detect"]))
    for display in found:
        caps = command_output(["ddcutil", "--bus", display.bus, "capabilities"])
        display.inputs = parse_input_sources(caps)
    return found


def switch_argv(text: str, bus: str = "") -> List[str]:
    """The ddcutil command that selects an input, optionally on one bus."""
    command = ["ddcutil"]
    if bus:
        command.extend(["--bus", bus])
    command.extend(["setvcp", "60", vcp_text(text)])
    return command
