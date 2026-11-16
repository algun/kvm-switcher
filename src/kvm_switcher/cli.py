"""Watch USB devices and switch the monitor input when one of them appears.

One program for Linux, macOS, and Windows. Settings live in
~/.config/kvm-switcher/config.json (override with KVM_CONFIG).

  kvm-switcher                 watch (asks to install on first interactive run)
  kvm-switcher configure       pick devices, the monitor, and its input
  kvm-switcher status          show saved choices and whether the watcher is running
  kvm-switcher install         run at login
  kvm-switcher uninstall        stop the login entry, and ask what else to remove
  kvm-switcher uninstall --purge  remove the login entry, the settings, and the program
  kvm-switcher switch [code]   switch once, or use the saved input
  kvm-switcher check           report whether a newer version is published
  kvm-switcher update          replace this program with the published version
  kvm-switcher version         print the version
"""

from __future__ import annotations

import sys
from typing import List, Optional

from kvm_switcher import __version__, install_root, paths, purge, service
from kvm_switcher.config import (
    DisplayTarget,
    load_config,
    load_saved,
    parse_input_value,
    save_config,
)
from kvm_switcher.devices import catalog
from kvm_switcher.displays import FoundDisplay, discover_displays, vcp_text
# Reached through the module, never imported by name: the path a config lives at
# depends on the environment, and one patch point keeps that true for callers.
from kvm_switcher import paths
from kvm_switcher.watcher import format_status

__all__ = ["main", "configure", "ensure_config", "status"]


def prompt_manual_display(current: str, saved: Optional[DisplayTarget]) -> DisplayTarget:
    """Ask for an input code by hand, when no monitor reported its list."""
    while True:
        hint = " [{}]".format(current) if current else ""
        typed = input("Monitor input{}: ".format(hint)).strip()
        input_value = typed or current
        if not input_value:
            print("Enter the monitor input code, such as 0x0f or 15.")
            continue
        try:
            parse_input_value(input_value)
        except ValueError:
            print("Enter a number such as 0x0f or 15.")
            continue
        break
    tag = saved.tag if saved and saved.tag else ""
    if paths.platform_key() == "darwin":
        while True:
            hint = " [{}]".format(tag) if tag else ""
            answer = input("BetterDisplay tag{}: ".format(hint)).strip()
            tag = answer or tag
            if tag.isdigit():
                break
            print("Enter the BetterDisplay tag id.")
    return DisplayTarget(
        input=vcp_text(input_value),
        bus=saved.bus if saved else "",
        tag=tag,
        name=saved.name if saved else "",
        label=saved.label if saved and saved.input == vcp_text(input_value) else "",
    )


def prompt_display(
    displays: List[FoundDisplay], saved: Optional[DisplayTarget]
) -> DisplayTarget:
    """Pick the monitor, then its input; fall back to typing a code."""
    current = saved.input if saved and saved.input else ""
    if not displays:
        print("\nNo monitor reported its input list. Type the input code.")
        return prompt_manual_display(current, saved)
    chosen = displays[0]
    if len(displays) > 1:
        print("\nWhich monitor should switch?\n")
        for index, display in enumerate(displays, start=1):
            where = "bus {}".format(display.bus) if display.bus else display.tag
            print("  {:2}  {}  {}".format(index, display.name, where).rstrip())
        print()
        while True:
            answer = input("Monitor: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(displays):
                chosen = displays[int(answer) - 1]
                break
            print("Enter a number from 1 to {}.".format(len(displays)))
    if not chosen.inputs:
        target = prompt_manual_display(current, saved)
        target.bus = chosen.bus or target.bus
        target.tag = chosen.tag or target.tag
        target.name = chosen.name or target.name
        return target
    print("\nInput on {}:\n".format(chosen.name))
    for index, (value, label) in enumerate(chosen.inputs, start=1):
        print("  {:2}  {}  {}".format(index, value, label).rstrip())
    print()
    while True:
        answer = input("Input (number, or a code such as 0x0f): ").strip()
        if not answer:
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(chosen.inputs):
            value, label = chosen.inputs[int(answer) - 1]
            return DisplayTarget(
                input=value, label=label, bus=chosen.bus, tag=chosen.tag, name=chosen.name
            )
        try:
            parse_input_value(answer)
        except ValueError:
            print(
                "Enter a number from 1 to {}, or a code such as 0x0f.".format(len(chosen.inputs))
            )
            continue
        return DisplayTarget(
            input=vcp_text(answer), bus=chosen.bus, tag=chosen.tag, name=chosen.name
        )


def configure() -> None:
    """Interactively choose the devices and the monitor input, then save them."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("Device selection needs a terminal. Run: kvm-switcher configure")
    path = paths.config_path()
    saved_config = load_saved(path)
    saved = saved_config.devices
    items = catalog(saved)
    if not items:
        raise SystemExit("No USB devices found.")
    selected = {vidpid for vidpid, _ in saved}
    while True:
        print("\nNumbers toggle devices. a selects all, n selects none.")
        print("s saves, q cancels. Any selected device switches the monitor.\n")
        for index, (vidpid, name) in enumerate(items, start=1):
            mark = "[x]" if vidpid in selected else "[ ]"
            print("  {} {:2}  {}  {}".format(mark, index, vidpid, name))
        print()
        answer = input("Choice: ").strip()
        if answer.lower() == "s":
            break
        if answer.lower() == "q":
            raise SystemExit("Device selection cancelled.")
        if answer.lower() == "a":
            selected = {vidpid for vidpid, _ in items}
            continue
        if answer.lower() == "n":
            selected = set()
            continue
        for token in answer.replace(",", " ").split():
            if not token.isdigit():
                continue
            number = int(token)
            if number < 1 or number > len(items):
                continue
            vidpid = items[number - 1][0]
            if vidpid in selected:
                selected.remove(vidpid)
            else:
                selected.add(vidpid)
    chosen = [vidpid for vidpid, _ in items if vidpid in selected]
    display = prompt_display(discover_displays(), saved_config.display)
    destination = paths.config_destination(path)
    save_config(destination, chosen, items, display)
    print("Saved {} device(s) to {}".format(len(chosen), destination))
    for vidpid in chosen:
        print("  {}".format(vidpid))
    label = " " + display.label if display.label else ""
    where = display.name or ("bus {}".format(display.bus) if display.bus else "")
    print("  input {}{}".format(display.input, label) + (" on " + where if where else ""))


def ensure_config() -> None:
    """Configure interactively, or copy the seeded config, when there is no config."""
    path = paths.config_path()
    if load_config(path).devices:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        configure()
        return
    seed = install_root.seed_path()
    if seed and not path.is_file() and not paths.legacy_config_path(path).is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")


def status() -> None:
    """Print the saved choices and whether the watcher is installed and running."""
    print(format_status(load_config(paths.config_path()), service.installed(), service.running()))


def install() -> None:
    """Register the login watcher for this installation, and say what to do next."""
    service.install()
    print("Installation complete. The watcher is running in the background.")
    print("Change the device pool with: kvm-switcher configure")
    print("Remove it with: kvm-switcher uninstall")


def ask_yes_no(question: str, default: bool = False) -> bool:
    """A y/N prompt, defaulting when the answer is empty.

    Only asked when both streams are a terminal; a piped or scripted run gets
    the default so it can never block waiting for input that will not come.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(question + suffix).strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def uninstall(argv: Optional[List[str]] = None) -> None:
    """Stop the login watcher and remove its service entry.

    Then asks what else to remove. Settings are the saved devices and monitor
    input; the program is the installed archive and the launcher on PATH. Both
    are separate questions because losing settings means re-picking hardware,
    while losing the program just means re-running the installer.
    """
    args = list(argv or [])
    keep_settings = "--keep-settings" in args
    purge_all = "--purge" in args

    removed = service.uninstall()
    if removed:
        print("Watcher removed. It will not start at login.")
    else:
        print("No watcher was installed.")

    if keep_settings:
        print("Config kept at {}".format(paths.config_path()))
        return

    if purge_all:
        drop_settings = True
        drop_program = True
    else:
        drop_settings = ask_yes_no("Remove your saved settings too?")
        drop_program = ask_yes_no("Remove the program itself too?")

    if drop_settings:
        removed_paths, failed = purge.remove_settings()
        for path in removed_paths:
            print("Removed {}".format(path))
        for path in failed:
            print("Could not remove {}".format(path), file=sys.stderr)
    else:
        print("Config kept at {}".format(paths.config_path()))

    if drop_program:
        if install_root.running_from_checkout():
            print(
                "This is a source checkout, so the program files are left alone; "
                "use git to remove them.",
                file=sys.stderr,
            )
        else:
            removed_paths, failed = purge.remove_program()
            for path in removed_paths:
                print("Removed {}".format(path))
            for path in failed:
                print("Could not remove {}".format(path), file=sys.stderr)
    elif drop_settings:
        print("Program kept. Remove it later with: kvm-switcher uninstall --purge")


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch a command line and return a process exit code."""
    if argv is None:
        argv = sys.argv
    command = argv[1] if len(argv) > 1 else "watch"
    if command in ("configure", "--configure", "-c"):
        configure()
    elif command in ("status", "--status"):
        status()
    elif command in ("install", "--install"):
        install()
    elif command in ("uninstall", "--uninstall", "remove", "--remove"):
        uninstall(argv[2:])
    elif command == "switch":
        from kvm_switcher.displays import switch_monitor

        value = argv[2] if len(argv) > 2 else None
        raise SystemExit(switch_monitor(value))
    elif command in ("watch",):
        from kvm_switcher.watcher import watch

        watch()
    elif command in ("check", "--check"):
        from kvm_switcher import update as update_module

        raise SystemExit(update_module.check())
    elif command in ("update", "--update", "upgrade"):
        from kvm_switcher import update as update_module

        raise SystemExit(update_module.update())
    elif command in ("version", "--version", "-V"):
        print(__version__)
    elif command in ("-h", "--help", "help"):
        print(__doc__)
    else:
        raise SystemExit("Unknown command {!r}. Try: kvm-switcher --help".format(command))
    return 0
