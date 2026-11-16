"""macOS monitor switching through the BetterDisplay CLI.

Display discovery stays manual on macOS: BetterDisplay owns the DDC channel, so
the program is given the tag id by hand at `configure` time rather than asking
the monitor itself.
"""

from __future__ import annotations

import shutil
from typing import List

from kvm_switcher.displays import vcp_text

BINARY_NAME = "betterdisplaycli"
BINARY_FALLBACK = "/opt/homebrew/bin/betterdisplaycli"


def binary() -> str:
    """The BetterDisplay CLI on PATH, or the Homebrew default."""
    return shutil.which(BINARY_NAME) or BINARY_FALLBACK


def switch_argv(text: str, tag: str, executable: str) -> List[str]:
    """The BetterDisplay command that selects an input on one tagged display."""
    return [
        executable, "set",
        "--tagID=" + tag,
        "--feature=ddc",
        "--vcp=inputSelect",
        "--value=" + vcp_text(text),
    ]
