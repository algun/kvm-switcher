"""The polling loop that watches USB devices and triggers a switch.

Includes the single-instance guard that refuses to start a second watcher: two
watchers would both switch on the same detection, which looks like a double
flash.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Iterable, List

from kvm_switcher import paths, service
from kvm_switcher.config import Config, load_config
from kvm_switcher.devices import connected_devices
from kvm_switcher.displays import switch_monitor
from kvm_switcher.process import (
    pid_start_time,
    process_alive,
    read_watcher_pid,
    read_watcher_start,
    write_watcher_pid,
)

INTERVAL = 0.5


def devices_present(wanted: Iterable[str]) -> bool:
    """True when any wanted vid:pid is on the bus right now."""
    wanted = list(wanted)
    if not wanted:
        return False
    present = {vidpid for vidpid, _ in connected_devices()}
    return any(vidpid in present for vidpid in wanted)


def format_status(config: Config, installed: bool, running: bool) -> str:
    """The `status` report: choices first, then whether the watcher is up."""
    lines: List[str] = []
    display = config.display
    configured = bool(config.devices) or bool(display and display.input)
    if not configured:
        lines.append("Not configured. Run: kvm-switcher configure")
    else:
        lines.append("Devices:")
        if config.devices:
            for vidpid, name in config.devices:
                lines.append("  {}  {}".format(vidpid, name))
        else:
            lines.append("  (none)")
        if display and display.input:
            label = " " + display.label if display.label else ""
            where = []
            if display.name:
                where.append(display.name)
            if display.bus:
                where.append("bus " + display.bus)
            if display.tag:
                where.append("tag " + display.tag)
            suffix = " on " + ", ".join(where) if where else ""
            lines.append("Display: {}{}{}".format(display.input, label, suffix))
        else:
            lines.append("Display: not chosen")
    if running:
        lines.append("Watcher: running")
    elif installed:
        lines.append("Watcher: installed, not running")
    else:
        lines.append("Watcher: not installed")
    return "\n".join(lines)


def maybe_install() -> None:
    """Offer to install the login watcher on the first interactive run."""
    if service.installed():
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    answer = input("Install the watcher to run at login? [y/N] ").strip()
    if answer.lower().startswith("y"):
        # The CLI owns what a user reads, including the closing three lines, so
        # the interactive path goes through it rather than duplicating the text.
        from kvm_switcher.cli import install as install_from_cli

        install_from_cli()
        raise SystemExit(0)
    print("Skipping installation. The watcher will run only in this window.")


def watch() -> None:
    """Run the watcher in this process, refusing to double up."""
    # Imported here because `cli` owns the interactive setup and imports this
    # module for the `watch` command; one direction must be deferred.
    from kvm_switcher.cli import ensure_config

    ensure_config()
    maybe_install()
    pid = os.getpid()
    # Two watchers would both switch on the same detection, which looks like a
    # double flash. Refuse to start when one is already running this install.
    existing = read_watcher_pid()
    if existing and existing != pid and process_alive(existing):
        raise SystemExit(
            "A watcher is already running (pid {}). "
            "Stop it first: kvm-switcher uninstall".format(existing)
        )
    write_watcher_pid(pid)
    try:
        _watch_loop()
    finally:
        # Only clear the record if it still identifies this exact process.
        if read_watcher_pid() == pid:
            recorded = read_watcher_start()
            if not recorded or pid_start_time(pid) == recorded:
                try:
                    paths.watcher_pid_path().unlink()
                except OSError:
                    pass


def _watch_loop() -> None:
    """Poll until interrupted, switching once per arrival rather than per poll."""
    active = False
    while True:
        wanted = [vidpid for vidpid, _ in load_config(paths.config_path()).devices]
        try:
            detected = devices_present(wanted)
        except (OSError, subprocess.CalledProcessError):
            detected = False
        if detected:
            if not active:
                # Set before switching: a failed switch must not leave the flag
                # False, or every poll would retry and flash the screen again.
                active = True
                print("USB device detected — switching display")
                try:
                    switch_monitor()
                except (OSError, subprocess.SubprocessError) as exc:
                    print("Switch failed: {}".format(exc), file=sys.stderr)
        else:
            active = False
        time.sleep(INTERVAL)
