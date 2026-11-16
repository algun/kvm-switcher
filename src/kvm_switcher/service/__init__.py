"""Run-at-login service registration, one module per platform mechanism.

The unified rule: the service always targets *this* installation, resolved
through :mod:`kvm_switcher.install_root`. It never copies the program somewhere
else and registers that copy, which is what left two installs behind and let
`update` refresh only the one it was invoked from.
"""

from __future__ import annotations

from kvm_switcher import paths


def _native():
    """The per-platform registration module for the running OS."""
    key = paths.platform_key()
    if key == "linux":
        from kvm_switcher.service import systemd

        return systemd
    if key == "darwin":
        from kvm_switcher.service import launchd

        return launchd
    if key == "windows":
        from kvm_switcher.service import windows

        return windows
    raise SystemExit("No service install for {}".format(key))


def install() -> None:
    """Register the login service for this installation and start it."""
    _native().install()


def uninstall() -> None:
    """Stop the login watcher and delete its service entry."""
    _native().uninstall()


def installed() -> bool:
    """True when a login entry for this user exists in any recognised form."""
    return _native().installed()


def running() -> bool:
    """True when the login watcher is running now."""
    return _native().running()


def parse_systemctl_is_active(text: str) -> bool:
    """`systemctl is-active` prints exactly "active" when the unit is up."""
    return text.strip() == "active"


def parse_launchctl_list(text: str) -> bool:
    """`launchctl list` prints a pid in the first column when the job is running."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or "kvm-switcher" not in parts[-1]:
            continue
        return parts[0].isdigit()
    return False


def parse_schtasks_status(text: str) -> bool:
    """The Status field of `schtasks /Query /FO LIST`."""
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "status":
            return value.strip().lower() == "running"
    return False
