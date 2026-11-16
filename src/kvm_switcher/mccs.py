"""MCCS VCP input codes: names, labels, and capability-string parsing.

Shared by the Linux ``ddcutil`` path and the Windows DDC path so the input
table exists once.
"""

from __future__ import annotations

import re
from typing import List, Tuple

MCCS_INPUT_NAMES = {
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

_MCCS_INPUT_RE = re.compile(r"\b60\s*\(([^)]*)\)", re.I)
_INPUT_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{2})\s*:\s*(.+)$")


def input_label(code: str) -> str:
    """Human name for an MCCS input-select value, or "" when it is not a known one."""
    return MCCS_INPUT_NAMES.get(code.strip().lower().removeprefix("0x"), "")


def parse_mccs_inputs(output: str) -> List[Tuple[str, str]]:
    """Input values from an MCCS capability string such as `vcp(... 60(11 12 0F 10) ...)`."""
    mccs = _MCCS_INPUT_RE.search(output)
    if not mccs:
        return []
    sources: List[Tuple[str, str]] = []
    for token in mccs.group(1).split():
        if not re.fullmatch(r"[0-9a-fA-F]{2}", token):
            continue
        code = token.lower()
        sources.append(("0x" + code, input_label(code)))
    return sources


def parse_input_sources(output: str) -> List[Tuple[str, str]]:
    """Input-select values from `ddcutil capabilities` or an MCCS capability string."""
    sources: List[Tuple[str, str]] = []
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
            sources.append(("0x" + match.group(1).lower(), match.group(2).strip()))
            continue
        if line:
            break
    if sources:
        return sources
    return parse_mccs_inputs(output)
