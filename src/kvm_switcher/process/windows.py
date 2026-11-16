"""Process primitives for Windows (``kernel32``), plus the WMI command line query.

Every ``ctypes.WinDLL`` binding is inside a function: this module is only ever
imported on Windows, but keeping the bindings local means the rule is uniform
and an accidental import elsewhere cannot fail.
"""

from __future__ import annotations

import ctypes
import subprocess

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
STILL_ACTIVE = 259


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def pid_start_time(pid: int) -> str:
    """Creation time from GetProcessTimes, so a recycled pid is not mistaken for ours."""
    kernel32 = _kernel32()
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


def process_alive(pid: int) -> bool:
    """True while the process has not exited (its exit code is still STILL_ACTIVE)."""
    kernel32 = _kernel32()
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


def terminate(pid: int) -> bool:
    """TerminateProcess the pid, reporting whether it was accepted."""
    kernel32 = _kernel32()
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(ctypes.c_void_p(handle), 1))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_command(pid: int) -> str:
    """Command line of a process. Empty when it cannot be read."""
    # WMI is the only way without extra dependencies; keep it short so a slow
    # provider cannot stall an update.
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Process -Filter 'ProcessId={}' "
                "-ErrorAction SilentlyContinue).CommandLine".format(int(pid)),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return output.strip()
