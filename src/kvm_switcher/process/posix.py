"""Process primitives for Linux and macOS (``/proc`` and ``os.kill``)."""

from __future__ import annotations

import os
import signal
from pathlib import Path


def pid_start_time(pid: int) -> str:
    """Field 22 of /proc/<pid>/stat (starttime), or "" when unreadable."""
    try:
        stat = Path("/proc/{}/stat".format(pid)).read_text(encoding="utf-8")
        return stat.split(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def process_alive(pid: int) -> bool:
    """True when the pid exists and is signalable by us."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate(pid: int) -> bool:
    """SIGTERM the process, reporting whether it was delivered."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def process_command(pid: int) -> str:
    """The argv of a process without the interpreter, or "" when unreadable."""
    try:
        raw = Path("/proc/{}/cmdline".format(pid)).read_bytes()
    except OSError:
        return ""
    parts = [part for part in raw.decode("utf-8", "replace").split("\x00") if part]
    return " ".join(parts[1:])
