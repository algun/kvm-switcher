"""Shared pytest fixtures.

The tests target the package modules directly, so the fixtures here exist to
remove repetition rather than to hide layout: `tmp_config` replaces the three
`TemporaryDirectory` blocks, `fake_monitors` replaces the hand-rolled dxva2
double, and `make_platform` is the one supported way to run an OS-specific
branch on a different platform.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kvm_switcher import paths  # noqa: E402  (after the sys.path fix above)
from kvm_switcher.displays import windows as windows_displays  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_hardware(monkeypatch):
    """Never let a test reach the real USB bus or real DDC devices.

    A test that wants a device list patches `connected_devices` itself; this
    guard is only so a test that forgot cannot enumerate hardware, stall on a
    PowerShell query, or write to a monitor. The opt-in live scan skips it,
    because reading the real bus is exactly what that test exists to do.
    """
    if os.environ.get("KVM_CHARACTERISATION_LIVE") == "1":
        return
    from kvm_switcher.devices import darwin as devices_darwin
    from kvm_switcher.devices import linux as devices_linux
    from kvm_switcher.devices import windows as devices_windows
    from kvm_switcher.displays import linux as displays_linux
    from kvm_switcher.displays import windows as displays_windows

    monkeypatch.setattr(devices_linux, "connected_devices", lambda: [])
    monkeypatch.setattr(devices_darwin, "connected_devices", lambda: [])
    monkeypatch.setattr(devices_windows, "connected_devices", lambda: [])
    monkeypatch.setattr(displays_linux, "command_output", lambda _command: "")
    monkeypatch.setattr(displays_windows, "_windows_physical_monitors", _no_monitors)


def _no_monitors():
    raise OSError("monitor enumeration disabled in tests")


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """An empty config file in a temporary directory, already on KVM_CONFIG."""
    path = tmp_path / "config.json"
    monkeypatch.setenv("KVM_CONFIG", str(path))
    return path


@pytest.fixture
def write_config(tmp_config):
    """Write a config file the way `configure` would, and return its path."""

    def write(devices=(), display=None):
        payload = {"devices": [{"id": vidpid, "name": name} for vidpid, name in devices]}
        if display is not None:
            payload["display"] = display
        tmp_config.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return tmp_config

    return write


class FakeDxva2:
    """Stands in for dxva2.dll: records writes, never talks to hardware."""

    def __init__(self, ok=True):
        self.writes = []
        self._ok = ok

    def SetVCPFeature(self, handle, code, value):
        self.writes.append((handle, code, value))
        return self._ok


@pytest.fixture
def fake_monitors(monkeypatch):
    """Make the Windows monitor list whatever the test says it is.

    Returns a factory: pass the descriptions Windows would report and it hands
    back the fake dxva2, so a test can assert exactly what was written.
    """

    def install(descriptions, ok=True):
        dxva2 = FakeDxva2(ok)
        collected = [
            (description, "handle{}".format(index), object(), 1)
            for index, description in enumerate(descriptions)
        ]
        monkeypatch.setattr(
            windows_displays, "_windows_physical_monitors", lambda: (dxva2, collected)
        )
        monkeypatch.setattr(windows_displays, "_release_windows_monitors", lambda *_a: None)
        return dxva2

    return install


@pytest.fixture
def make_platform(monkeypatch):
    """Run a dispatching function as if it were on another OS.

    Only the platform key is replaced, so the branch under test and the module
    it dispatches to are the real ones.
    """

    def use(key):
        monkeypatch.setattr(paths, "platform_key", lambda: key)
        return key

    return use
