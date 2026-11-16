"""Process helpers: spawning, single-instance checks, and killing a watcher.

The pid file handling, the "is this still our process" check and the spawn
wrapper live here once; only the OS primitives (OpenProcess versus ``kill``)
are per-platform, in ``posix`` and ``windows``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from kvm_switcher import paths


def _native():
    """The per-platform primitives module for the running OS."""
    if sys.platform == "win32":
        from kvm_switcher.process import windows

        return windows
    from kvm_switcher.process import posix

    return posix


def pid_start_time(pid: int) -> str:
    """Creation time of a live process, so a recycled pid is not mistaken for ours."""
    return _native().pid_start_time(pid)


def process_alive(pid: int) -> bool:
    """True when a process with this pid exists and has not exited."""
    if pid <= 0:
        return False
    return _native().process_alive(pid)


def write_watcher_pid(pid: int) -> None:
    """Record the watcher's pid and start time, best effort."""
    start = pid_start_time(pid)
    try:
        path = paths.watcher_pid_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n{}\n".format(pid, start), encoding="utf-8")
    except OSError:
        pass


def read_watcher_pid() -> Optional[int]:
    """The recorded watcher pid, or None when there is no readable record."""
    try:
        first = paths.watcher_pid_path().read_text(encoding="utf-8").strip().splitlines()[0]
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


def read_watcher_start() -> str:
    """The start time recorded next to the pid, or "" when absent."""
    try:
        lines = paths.watcher_pid_path().read_text(encoding="utf-8").strip().splitlines()
        return lines[1].strip() if len(lines) > 1 else ""
    except OSError:
        return ""


def stop_watcher_process() -> bool:
    """Stop a watcher this user started, but never an unrelated process that
    happens to have inherited its pid after the watcher exited."""
    pid = read_watcher_pid()
    recorded = read_watcher_start()
    stopped = False
    try:
        if pid and process_alive(pid) and (not recorded or pid_start_time(pid) == recorded):
            stopped = _native().terminate(pid)
    finally:
        try:
            paths.watcher_pid_path().unlink()
        except OSError:
            pass
    return stopped


def running_watcher_target() -> str:
    """Command target of the watcher this user is running, or "" when unknown."""
    pid = read_watcher_pid()
    if not pid or not process_alive(pid):
        return ""
    return _native().process_command(pid)


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


def spawn_watcher(argv: List[str], detach: bool = True) -> int:
    """Start a watcher as a child process.

    ``detach`` keeps its stdio off the caller's pipes. That is required for
    ``update``, which may run with captured output and would otherwise wait on
    them forever; ``install`` keeps the terminal attached, as it always has, so
    a new watcher's first log lines land where the user ran the command.
    """
    if detach:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    else:
        process = subprocess.Popen(list(argv), close_fds=True)
    return process.pid
