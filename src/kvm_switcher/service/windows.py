"""Windows run-at-login registration: a Scheduled Task, else the Run key.

``schtasks`` needs elevation on some machines, so a failure falls back to the
per-user ``Run`` key, which never does. Both point at this installation.
"""

from __future__ import annotations

import subprocess
import sys

from kvm_switcher import install_root
from kvm_switcher.process import read_watcher_pid, process_alive, spawn_watcher
from kvm_switcher.process import write_watcher_pid
from kvm_switcher.service import parse_schtasks_status

TASK_NAME = "kvm-switcher"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def command() -> str:
    """The command line a task or Run-key entry stores.

    Background, because this runs at logon: a console interpreter would put a
    terminal panel on screen whose window closes the watcher when closed.
    """
    return subprocess.list2cmdline(install_root.program_argv(background=True))


def install() -> None:
    created = subprocess.run(
        [
            "schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
            "/TN", TASK_NAME, "/TR", command(),
        ]
    )
    if created.returncode != 0:
        # A Scheduled Task needs elevation on some machines. The per-user
        # Run key does not, so it is the fallback rather than a failure.
        print(
            "Scheduled Task could not be created "
            "(schtasks exit {}); using the per-user Run key instead.".format(created.returncode),
            file=sys.stderr,
        )
        _write_run_key()
    write_watcher_pid(spawn_watcher(install_root.program_argv(background=True), detach=False))


def _write_run_key() -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as run_key:
        winreg.SetValueEx(run_key, TASK_NAME, 0, winreg.REG_SZ, command())


def _delete_run_key() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as run_key:
            winreg.DeleteValue(run_key, TASK_NAME)
        return True
    except OSError:
        return False


def _run_key_present() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as run_key:
            winreg.QueryValueEx(run_key, TASK_NAME)
        return True
    except OSError:
        return False


def uninstall() -> bool:
    """Stop the watcher and remove both possible login entries.

    schtasks may not own the entry at all (the Run-key fallback does), so
    success is only reported for something that really changed.
    """
    from kvm_switcher.process import stop_watcher_process

    stopped = stop_watcher_process()
    ended = subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], check=False)
    deleted = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], check=False)
    removed = ended.returncode == 0 or deleted.returncode == 0
    if _delete_run_key():
        removed = True
    return removed or stopped


def installed() -> bool:
    task = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if task.returncode == 0:
        return True
    return _run_key_present()


def running() -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if parse_schtasks_status(result.stdout):
        return True
    pid = read_watcher_pid()
    return bool(pid and process_alive(pid))
