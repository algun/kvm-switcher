"""Tests that do not need a monitor, USB bus, or login service.

Hardware and OS integration (ddcutil, BetterDisplay, Windows VCP, systemd,
launchd, schtasks) stay manual. See README.

Every assertion here was carried over from the single-file suite unchanged. The
only edits are structural: fixtures replace the temporary directories and the
hand-rolled dxva2 double, `parametrize` replaces the copy-pasted case tables,
and `patch.object(ks, ...)` is replaced by patching the module that now owns
the name - which is the point of the split, not a relaxation of the test.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import pytest
except ImportError:  # pragma: no cover - a runner without pytest, e.g. unittest
    # This module is a pytest suite. CI's fallback runner is `unittest discover`,
    # which imports every test file; skipping with a reason keeps that green
    # without silencing a single assertion when the suite actually runs.
    import unittest

    raise unittest.SkipTest("this module requires pytest")

import kvm_switcher
from kvm_switcher import cli, install_root, mccs, paths, update, watcher
from kvm_switcher.config import (
    Config,
    DisplayTarget,
    load_config,
    parse_input_value,
    parse_json_config,
    parse_legacy,
    save_config,
)
from kvm_switcher.devices import catalog
from kvm_switcher.devices import darwin as devices_darwin
from kvm_switcher.devices import linux as devices_linux
from kvm_switcher.devices import windows as devices_windows
from kvm_switcher.displays import darwin as displays_darwin
from kvm_switcher.displays import linux as displays_linux
from kvm_switcher.displays import windows as displays_windows
from kvm_switcher.displays import monitor_matches, switch_monitor, vcp_text
from kvm_switcher.service import parse_launchctl_list, parse_schtasks_status, parse_systemctl_is_active

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_legacy_text_and_json(tmp_path):
    legacy = tmp_path / "devices.txt"
    legacy.write_text(
        "# comment\n\ninput 0x0f\ntag 42\n1234:abcd  # Receiver\n1234:0001\n",
        encoding="utf-8",
    )
    loaded = parse_legacy(legacy)
    assert loaded.devices == [("1234:abcd", "Receiver"), ("1234:0001", "1234:0001")]
    assert loaded.display.input == "0x0f"
    assert loaded.display.tag == "42"

    path = tmp_path / "config.json"
    path.write_text(
        '{"devices":[{"id":"1234:ABCD","name":"Receiver"}],'
        '"display":{"name":"Monitor","bus":"5","input":"0x0f","inputLabel":"DisplayPort-1"}}\n',
        encoding="utf-8",
    )
    loaded = parse_json_config(path)
    assert loaded.devices == [("1234:abcd", "Receiver")]
    assert loaded.display.bus == "5"
    assert loaded.display.label == "DisplayPort-1"


def test_missing_file_is_empty():
    loaded = load_config(Path("/no/such/config.json"))
    assert loaded.devices == []
    assert loaded.display is None


def test_json_falls_back_to_legacy_sibling(tmp_path):
    legacy = tmp_path / "devices.txt"
    legacy.write_text("1234:abcd  # Receiver\ninput 0x11\n", encoding="utf-8")
    loaded = load_config(tmp_path / "config.json")
    assert loaded.devices == [("1234:abcd", "Receiver")]
    assert loaded.display.input == "0x11"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0x0F", 15),
        ("15", 15),
    ],
)
def test_input_value_hex_and_decimal(text, expected):
    assert parse_input_value(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("15", "0xf"),
        ("0x0f", "0x0f"),
    ],
)
def test_vcp_text_preserves_hex_and_converts_decimal(text, expected):
    assert vcp_text(text) == expected


def test_input_value_rejects_junk():
    with pytest.raises(ValueError):
        parse_input_value("hdmi")


def test_input_comes_only_from_config():
    from kvm_switcher.config import effective_input

    assert effective_input(None) is None
    assert effective_input("0x12") == "0x12"


def test_catalog_keeps_saved_devices_that_are_unplugged(monkeypatch):
    from kvm_switcher import devices

    saved = [("1234:abcd", "Receiver"), ("1234:0001", "Mic")]
    monkeypatch.setattr(devices, "connected_devices", lambda: [("1234:abcd", "Receiver live")])
    items = catalog(saved)
    assert items[0] == ("1234:abcd", "Receiver live")
    assert items[1] == ("1234:0001", "Mic (not connected)")


def test_save_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    items = [("1234:abcd", "Receiver (not connected)")]
    display = DisplayTarget(input="0x11", label="HDMI-1", bus="5", tag="42", name="Monitor")
    save_config(path, ["1234:abcd"], items, display)
    loaded = parse_json_config(path)
    assert loaded.devices == [("1234:abcd", "Receiver")]
    assert loaded.display.input == "0x11"
    assert loaded.display.bus == "5"
    assert loaded.display.tag == "42"
    assert loaded.display.label == "HDMI-1"


# --------------------------------------------------------------------------
# Device listing
# --------------------------------------------------------------------------


def test_lsusb_skips_root_hubs_and_duplicates():
    output = (
        "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
        "Bus 001 Device 004: ID 1234:ABCD USB Receiver\n"
        "Bus 002 Device 003: ID 1234:abcd USB Receiver\n"
        "noise\n"
    )
    assert devices_linux.parse_lsusb(output) == [("1234:abcd", "USB Receiver")]


def test_ioreg_pairs_vendor_and_product():
    output = """
+-o AppleUSBXHCI Root Hub@0
  "idVendor" = 0x1d6b
  "idProduct" = 0x0002
+-o Camera@1
  "idVendor" = 0x1234
  "idProduct" = 0x2
"""
    found = devices_darwin.parse_ioreg(output)
    assert found == [("1d6b:0002", "AppleUSBXHCI Root Hub"), ("1234:0002", "Camera")]


def test_windows_lines():
    output = "1234:abcd\tUSB Receiver\nnot-a-device\n1234:abcd\tagain\n"
    assert devices_windows.parse_windows_devices(output) == [("1234:abcd", "USB Receiver")]


def test_windows_parses_literal_backtick_t():
    # Regression: PowerShell only expands `t inside a double-quoted format
    # string. A single-quoted one emits backtick+t literally, and requiring a
    # real tab there made every device parse away, so the watcher never fired.
    output = "1234:abcd`tUSB Receiver\n5678:ef01`tKeyboard\n"
    assert devices_windows.parse_windows_devices(output) == [
        ("1234:abcd", "USB Receiver"),
        ("5678:ef01", "Keyboard"),
    ]


def test_windows_script_format_string_is_double_quoted():
    script = devices_windows.windows_device_script()
    # The tab lives in the output format string, which must be double-quoted:
    # PowerShell only expands `t there. A single-quoted one emitted "`t".
    assert '"{0}`t{1}"' in script
    assert '"{0}:{1}`t' not in script
    # The product string is what makes a device identifiable; the old query
    # only read FriendlyName and the USB class name.
    assert "BusReportedDeviceDesc" in script
    assert "-notmatch '&MI_'" not in script


@pytest.mark.parametrize(
    "name",
    [
        "USB Composite Device",
        "USB Input Device",
        "Generic USB Hub",
        "usb composite device",
        "",
        "   ",
    ],
)
def test_generic_names_are_not_useful(name):
    assert not devices_windows.useful_device_name(name), name


@pytest.mark.parametrize(
    "name",
    ["Fiction Microphone V3", "Fiction Numpad 18", "Fiction USB2.1 Hub"],
)
def test_real_names_are_useful(name):
    assert devices_windows.useful_device_name(name), name


# --------------------------------------------------------------------------
# Display discovery
# --------------------------------------------------------------------------


def test_detect_skips_invalid_and_reads_model():
    output = """
Invalid display
   I2C bus:  /dev/i2c-3
   Model:                DELL U2720Q

Display 1
   I2C bus:  /dev/i2c-5
   EDID synopsis:
      Model:                Monitor
"""
    found = displays_linux.parse_ddcutil_detect(output)
    assert len(found) == 1
    assert found[0].bus == "5"
    assert found[0].name == "Monitor"


def test_capabilities_and_mccs():
    caps = """
  Feature: 60 (Input Source)
      Values:
         0f: DisplayPort-1
         11: HDMI-1
  Feature: 62 (Audio)
"""
    assert mccs.parse_input_sources(caps) == [("0x0f", "DisplayPort-1"), ("0x11", "HDMI-1")]
    assert mccs.parse_input_sources("vcp(10 60(0F 12) )") == [
        ("0x0f", "DisplayPort-1"),
        ("0x12", "HDMI-2"),
    ]


def test_windows_capability_string_gives_labelled_inputs():
    # The exact shape a monitor returns through DXVA2 capabilities.
    caps = (
        "(prot(monitor)type(lcd)model(SAMPLE)cmds(01 02 03 07 0C 4E F3 E3)"
        "vcp(02 04 05 08 0B 0C 10 12 60(11 12 0F 10) 62 8D FF)mswhql(1)mccs_ver(2.0))"
    )
    assert mccs.parse_mccs_inputs(caps) == [
        ("0x11", "HDMI-1"),
        ("0x12", "HDMI-2"),
        ("0x0f", "DisplayPort-1"),
        ("0x10", "DisplayPort-2"),
    ]


def test_unknown_input_code_still_listed():
    # A monitor may advertise a value the table does not know; keep the code
    # rather than dropping the choice.
    assert mccs.parse_mccs_inputs("vcp(60(11 7a))") == [("0x11", "HDMI-1"), ("0x7a", "")]


@pytest.mark.parametrize(
    "code, expected",
    [
        ("0x0f", "DisplayPort-1"),
        ("0F", "DisplayPort-1"),
        ("12", "HDMI-2"),
        ("0x99", ""),
    ],
)
def test_input_labels(code, expected):
    assert mccs.input_label(code) == expected


@pytest.mark.parametrize(
    "description, wanted, expected",
    [
        ("MSI Monitor", "Monitor", True),
        ("DELL U2720Q", "Monitor", False),
        ("anything", "", True),
    ],
)
def test_monitor_name_match(description, wanted, expected):
    assert monitor_matches(description, wanted) is expected


def test_windows_generic_monitor_still_matches():
    # Regression: Windows reports many panels as "Generic PnP Monitor", so a
    # saved real model name never matched and switch exited with no monitor.
    assert displays_windows.windows_monitor_matches("Generic PnP Monitor", "Test Monitor 4K")
    assert displays_windows.windows_monitor_matches("Test Monitor 4K", "Test Monitor 4K")
    # A genuinely different, identifiable monitor must still be skipped.
    assert not displays_windows.windows_monitor_matches("DELL U2720Q", "Test Monitor 4K")
    assert displays_windows.windows_monitor_matches("DELL U2720Q", "")


# --------------------------------------------------------------------------
# Windows switch target
# --------------------------------------------------------------------------


def test_single_generic_monitor_is_switched(fake_monitors):
    dxva2 = fake_monitors(["Generic PnP Monitor"])
    assert displays_windows.switch(0x0F, "Test Monitor 4K") == 1
    assert dxva2.writes == [("handle0", 0x60, 0x0F)]


def test_two_monitors_with_unmatched_name_switches_nothing(fake_monitors):
    # Two displays, neither carrying the saved name: writing to either could
    # black out the wrong screen, so nothing may be touched.
    dxva2 = fake_monitors(["Generic PnP Monitor", "Generic PnP Monitor"])
    assert displays_windows.switch(0x0F, "Test Monitor 4K") == 0
    assert dxva2.writes == []


def test_named_match_still_switches_among_several(fake_monitors):
    dxva2 = fake_monitors(["DELL U2720Q", "Test Monitor 4K"])
    assert displays_windows.switch(0x0F, "Test Monitor 4K") == 1
    assert [handle for handle, _code, _value in dxva2.writes] == ["handle1"]


def test_failed_write_is_not_counted(fake_monitors):
    fake_monitors(["Generic PnP Monitor"], ok=False)
    assert displays_windows.switch(0x0F, "") == 0


# --------------------------------------------------------------------------
# Watcher process
# --------------------------------------------------------------------------


@pytest.fixture
def pid_file(tmp_config, monkeypatch):
    """A watcher pid file in the temp config directory, already wired up."""
    path = tmp_config.with_name("watcher.pid")
    monkeypatch.setattr(paths, "watcher_pid_path", lambda: path)
    return path


def test_pid_file_roundtrip(pid_file):
    from kvm_switcher.process import read_watcher_pid, write_watcher_pid

    write_watcher_pid(4321)
    assert read_watcher_pid() == 4321


def test_missing_or_junk_pid_file_is_none(pid_file):
    from kvm_switcher.process import read_watcher_pid

    assert read_watcher_pid() is None
    pid_file.write_text("not-a-number", encoding="utf-8")
    assert read_watcher_pid() is None


def test_dead_process_is_not_alive():
    from kvm_switcher.process import process_alive

    # A pid that cannot be running.
    assert not process_alive(0)
    assert not process_alive(-1)


def test_stop_clears_stale_pid_file(pid_file):
    # Regression: uninstall claimed success while the watcher kept running.
    from kvm_switcher.process import stop_watcher_process, write_watcher_pid

    write_watcher_pid(999999)
    assert not stop_watcher_process()
    assert not pid_file.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX child process")
def test_stop_refuses_a_recycled_pid(pid_file):
    # A pid file left behind by a crash must not let uninstall kill whatever
    # unrelated process later inherits that pid.
    from kvm_switcher.process import stop_watcher_process

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_file.write_text("{}\nnot-our-start-time\n".format(victim.pid), encoding="utf-8")
        assert not stop_watcher_process()
        assert victim.poll() is None, "an unrelated process was killed"
    finally:
        victim.kill()
        victim.wait()


# --------------------------------------------------------------------------
# switch command
# --------------------------------------------------------------------------


def test_linux_argv():
    assert displays_linux.switch_argv("0x0f") == ["ddcutil", "setvcp", "60", "0x0f"]
    assert displays_linux.switch_argv("0x0f", "5") == [
        "ddcutil", "--bus", "5", "setvcp", "60", "0x0f"
    ]


def test_darwin_argv():
    assert displays_darwin.switch_argv("17", "42", "/opt/homebrew/bin/betterdisplaycli") == [
        "/opt/homebrew/bin/betterdisplaycli", "set",
        "--tagID=42",
        "--feature=ddc",
        "--vcp=inputSelect",
        "--value=0x11",
    ]


def test_switch_requires_a_configured_input(monkeypatch):
    from kvm_switcher import displays

    monkeypatch.setattr(displays, "load_config", lambda _path: Config())
    with pytest.raises(SystemExit) as caught:
        switch_monitor()
    assert "No monitor input configured" in str(caught.value)


def test_switch_uses_built_command(monkeypatch, make_platform):
    from kvm_switcher import displays

    make_platform("linux")
    saved = Config(display=DisplayTarget(input="0x0f", bus="5", name="Monitor"))
    monkeypatch.setattr(displays, "load_config", lambda _path: saved)
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(displays.subprocess, "run", fake_run)
    assert switch_monitor("0x10") == 0
    assert calls == [["ddcutil", "--bus", "5", "setvcp", "60", "0x10"]]


def test_presence_is_any_match(monkeypatch):
    from kvm_switcher import watcher as watcher_module

    monkeypatch.setattr(
        watcher_module, "connected_devices", lambda: [("1234:abcd", "Receiver")]
    )
    assert watcher_module.devices_present(["1234:0001", "1234:abcd"])
    assert not watcher_module.devices_present(["1234:0001"])
    assert not watcher_module.devices_present([])


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_format_reports_choices_and_watcher():
    config = Config(
        devices=[("1234:abcd", "Receiver")],
        display=DisplayTarget(input="0x0f", label="DisplayPort-1", bus="5", name="Monitor"),
    )
    text = watcher.format_status(config, installed=True, running=True)
    assert "1234:abcd  Receiver" in text
    assert "Display: 0x0f DisplayPort-1 on Monitor, bus 5" in text
    assert "Watcher: running" in text
    assert "installed, not running" in watcher.format_status(config, True, False)
    assert "not installed" in watcher.format_status(Config(), False, False)
    assert "Not configured" in watcher.format_status(Config(), False, False)


@pytest.mark.parametrize(
    "text, expected",
    [("active\n", True), ("inactive\n", False)],
)
def test_parse_systemctl(text, expected):
    assert parse_systemctl_is_active(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("123\t0\tcom.kvm-switcher.watcher\n", True),
        ("-\t0\tcom.kvm-switcher.watcher\n", False),
    ],
)
def test_parse_launchctl(text, expected):
    assert parse_launchctl_list(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Status:    Running\n", True),
        ("Status:    Ready\n", False),
    ],
)
def test_parse_schtasks(text, expected):
    assert parse_schtasks_status(text) is expected


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_unknown_command():
    with pytest.raises(SystemExit) as caught:
        cli.main(["kvm-switcher.py", "nope"])
    assert "Unknown command" in str(caught.value)


def test_version_prints_constant(capsys):
    assert cli.main(["kvm-switcher.py", "version"]) == 0
    assert capsys.readouterr().out.strip() == kvm_switcher.__version__
    assert kvm_switcher.__version__.count(".") == 2


def test_help_prints_doc(capsys):
    cli.main(["kvm-switcher.py", "--help"])
    assert capsys.readouterr().out.strip() == cli.__doc__.strip()
    assert "Watch USB devices and switch the monitor input" in cli.__doc__


# --------------------------------------------------------------------------
# Shell scripts
# --------------------------------------------------------------------------


@pytest.mark.skipif(not __import__("shutil").which("bash"), reason="bash is not installed")
def test_bash_scripts_parse():
    scripts = sorted(ROOT.glob("*.sh"))
    assert len(scripts) >= 1
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)


# --------------------------------------------------------------------------
# Portability
# --------------------------------------------------------------------------


def test_import_survives_without_windows_ctypes_symbols():
    # ctypes.WINFUNCTYPE/WinDLL exist only on Windows. Binding one at module
    # level made the program raise AttributeError on Linux and macOS, which
    # broke every non-Windows install.
    code = (
        "import ctypes, sys\n"
        "for name in ('WINFUNCTYPE', 'WinDLL', 'windll', 'WINFUNCTYPE'):\n"
        "    if hasattr(ctypes, name):\n"
        "        delattr(ctypes, name)\n"
        "sys.path.insert(0, {src!r})\n"
        "import kvm_switcher\n"
        "import pkgutil, importlib\n"
        "for info in pkgutil.walk_packages(kvm_switcher.__path__, 'kvm_switcher.'):\n"
        "    importlib.import_module(info.name)\n"
        "from kvm_switcher.cli import main\n"
        "print('ok')\n"
    ).format(src=str(SRC))
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=90
    )
    assert result.returncode == 0, (
        "package failed to import without Windows ctypes symbols:\n" + result.stderr
    )


def test_no_windows_binding_at_module_level():
    # Inside a function is fine (it only runs on Windows); at module scope it
    # runs on every import. Only top-level expressions are inspected.
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # bodies only execute when called
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in {
                    "WinDLL", "WINFUNCTYPE", "windll"
                }:
                    offenders.append((path.name, sub.attr, getattr(sub, "lineno", 0)))
    assert offenders == [], "Windows-only ctypes call at module scope"


# --------------------------------------------------------------------------
# Watch loop
# --------------------------------------------------------------------------


def _patch_loop(monkeypatch, sequence, switch):
    """Wire the loop to a scripted presence sequence that ends in StopIteration."""
    ticks = []

    def sleep(_seconds):
        ticks.append(1)

    def present(_wanted):
        if not sequence:
            raise StopIteration
        return sequence.pop(0)

    monkeypatch.setattr(watcher, "load_config", lambda _path: Config(devices=[("1234:abcd", "Dev")]))
    monkeypatch.setattr(watcher, "devices_present", present)
    monkeypatch.setattr(watcher, "switch_monitor", switch)
    monkeypatch.setattr(watcher.time, "sleep", sleep)
    return ticks


def test_switch_is_retried_only_after_the_device_goes_away(monkeypatch, capsys):
    """A failed or repeated poll must not re-switch while the device stays."""
    calls = []
    _patch_loop(monkeypatch, [True, True, False, True, True], lambda *a: calls.append(1))
    with pytest.raises(StopIteration):
        watcher._watch_loop()
    # True,True (one switch) then False then True,True (second switch)
    assert len(calls) == 2


def test_failed_switch_does_not_retry_every_poll(monkeypatch, capsys):
    """If switching raises, the flag must stay set so the screen does not
    flash once per poll."""
    calls = []

    def boom(*_a):
        calls.append(1)
        raise OSError("ddc failed")

    _patch_loop(monkeypatch, [True, True, True], boom)
    with pytest.raises(StopIteration):
        watcher._watch_loop()
    assert len(calls) == 1, "switch retried while the device stayed present"
    assert "Switch failed" in capsys.readouterr().err


def test_second_watcher_refuses_to_start(pid_file, monkeypatch):
    class FakeOs:
        @staticmethod
        def getpid():
            return 999

    pid_file.write_text("4242\n", encoding="utf-8")
    monkeypatch.setattr(watcher, "os", FakeOs)
    monkeypatch.setattr(watcher, "process_alive", lambda _pid: True)
    # watch() asks cli for the interactive setup, so that is where it is patched.
    monkeypatch.setattr(cli, "ensure_config", lambda: None)
    monkeypatch.setattr(watcher, "maybe_install", lambda: None)
    with pytest.raises(SystemExit) as caught:
        watcher.watch()
    assert "already running" in str(caught.value)


# --------------------------------------------------------------------------
# Update
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, current, expected",
    [
        ("0.1.7", "0.1.6", True),
        ("0.1.6", "0.1.6", False),
        ("0.1.5", "0.1.6", False),
        ("v0.2.0", "0.1.9", True),
        # Numeric, not string, comparison: "10" is newer than "9".
        ("0.1.10", "0.1.9", True),
        ("1.0", "0.9.9", True),
        ("0.1.6.1", "0.1.6", True),
        ("junk", "0.1.6", False),
        ("", "0.1.6", False),
    ],
)
def test_version_comparison(candidate, current, expected):
    assert update.version_is_newer(candidate, current) is expected


@pytest.mark.parametrize(
    "body, message",
    [
        (b'#!/usr/bin/env python3\nVERSION = "0.0.1"\n', "not newer"),
        (b'VERSION = "9.9.9"\ndef broken(:\n', "does not compile"),
        (b"<html>404</html>", "not kvm-switcher.py"),
        # A BOM breaks the shebang that install.sh symlinks onto PATH.
        (b'\xef\xbb\xbfVERSION = "9.9.9"\n', "BOM"),
    ],
)
def test_bad_download_is_rejected(body, message):
    with pytest.raises(SystemExit) as caught:
        update._verified_program(body, "0.1.6")
    assert message in str(caught.value)


def test_good_download_is_accepted():
    body = b'#!/usr/bin/env python3\nVERSION = "9.9.9"\n'
    assert update._verified_program(body, "0.1.6") == body


def test_checkout_refuses_to_self_update(monkeypatch):
    # A git checkout must be updated with git, never overwritten in place.
    monkeypatch.setattr(update, "running_from_checkout", lambda: True)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    with pytest.raises(SystemExit) as caught:
        update.update()
    assert "git pull" in str(caught.value)


def test_same_program_matches_quoted_and_absolute_paths():
    from kvm_switcher.process import same_program

    target = str(ROOT / "kvm-switcher.py")
    assert same_program(target, target)
    assert same_program('"{}"'.format(target), target)
    assert same_program("python.exe {}".format(target), target)
    assert not same_program("/somewhere/else/kvm-switcher.py", target)
    assert not same_program("", target)


def test_update_leaves_other_watchers_alone(tmp_path, monkeypatch, capsys):
    """A copy installed elsewhere must not stop a watcher it does not own."""
    from kvm_switcher import process

    target = tmp_path / "kvm-switcher.py"
    target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
    body = b'VERSION = "9.9.9"\n'
    stopped = []
    spawned = []

    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: body)
    monkeypatch.setattr(install_root, "program_path", lambda: target)
    monkeypatch.setattr(process, "read_watcher_pid", lambda: 4242)
    monkeypatch.setattr(process, "process_alive", lambda _pid: True)
    monkeypatch.setattr(
        process, "running_watcher_target", lambda: "/other/install/kvm-switcher.py"
    )
    monkeypatch.setattr(process, "stop_watcher_process", lambda: stopped.append(1) or True)
    monkeypatch.setattr(process, "spawn_watcher", lambda _argv: spawned.append(1) or 1234)

    assert update.update() == 0
    written = target.read_bytes()
    assert not stopped, "someone else's watcher was stopped"
    assert not spawned
    assert b"9.9.9" in written
    assert "A different watcher is running" in capsys.readouterr().out


def test_update_replaces_a_legacy_install_in_place(tmp_path, monkeypatch, capsys):
    """The single file a pre-package install ran is still the update target."""
    from kvm_switcher import process

    target = tmp_path / "kvm-switcher.py"
    target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
    body = b'VERSION = "9.9.9"\n'
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: body)
    monkeypatch.setattr(install_root, "program_path", lambda: target)
    monkeypatch.setattr(process, "running_watcher_target", lambda: "")

    assert update.update() == 0
    assert b"9.9.9" in target.read_bytes()
    assert not (tmp_path / "kvm-switcher.py.new").exists()
    assert "Updated" in capsys.readouterr().out


def test_update_refuses_a_directory_install_without_a_program_file(monkeypatch):
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(install_root, "program_path", lambda: None)
    with pytest.raises(SystemExit) as caught:
        update.update()
    assert "Re-run the installer" in str(caught.value)


def test_watcher_spawn_does_not_inherit_stdio(monkeypatch):
    # An inherited pipe made `update` hang when its output was captured.
    from kvm_switcher import process

    calls = []

    class FakeProcess:
        pid = 1234

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(process.subprocess, "Popen", fake_popen)
    assert process.spawn_watcher(["python", "program.py"]) == 1234
    argv, kwargs = calls[0]
    assert argv == ["python", "program.py"]
    assert kwargs["stdout"] == process.subprocess.DEVNULL
    assert kwargs["stderr"] == process.subprocess.DEVNULL
    assert kwargs["stdin"] == process.subprocess.DEVNULL


def test_install_watcher_keeps_the_terminal(monkeypatch):
    """install() leaves the child attached, which is what it always did."""
    from kvm_switcher import process

    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(process.subprocess, "Popen", fake_popen)
    assert process.spawn_watcher(["python", "program.py"], detach=False) == 4321
    _argv, kwargs = calls[0]
    assert "stdout" not in kwargs
    assert "stdin" not in kwargs


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

_MOJIBAKE = (
    "[\u00c2\u00c3\u00e2]"
    "[\u0080-\u00ff\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192"
    "\u02c6\u02dc\u2013-\u203a\u20ac\u2122\ufffd]"
)


def _looks_mangled(text):
    import re

    if text.startswith("\ufeff"):
        return True
    return bool(re.search(_MOJIBAKE, text))


# What counts as a text file worth scanning. Suffixes are compared lowercase, and
# names separately, because tracked files include `.gitignore` and `Pipfile`.
_TEXT_SUFFIXES = {
    ".py", ".md", ".ps1", ".sh", ".json", ".yml", ".yaml", ".toml", ".cfg", ".txt",
}
_TEXT_NAMES = {".gitignore", ".gitattributes", "Pipfile"}


def _is_text_file(path):
    return path.suffix.lower() in _TEXT_SUFFIXES or path.name in _TEXT_NAMES


def _tracked_files():
    """The files git tracks, or None when git cannot answer.

    None means "cannot tell", never "nothing tracked", so the caller can skip
    instead of substituting a weaker scan. That distinction is the whole point:
    the fallback this replaces walked the entire working tree, which on a
    developer machine means `.venv`, `dist/`, `__pycache__` and every gitignored
    scratch file. So the test could fail for a reason that has nothing to do with
    the repository, and its reach depended on whether a binary happened to be on
    PATH.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        listing = subprocess.run(
            [git, "ls-files", "-z"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # A non-zero exit means "not a checkout" or a broken repository, not "no
    # files": either way git has not answered the question.
    if listing.returncode != 0:
        return None
    names = [name for name in listing.stdout.decode("utf-8", "replace").split("\0") if name]
    return [ROOT / name for name in names]


def _mangled_files(paths, skip=()):
    """The names of files in `paths` that look mangled or BOM-prefixed."""
    skip = {Path(path).resolve() for path in skip}
    offenders = []
    for path in paths:
        if not path.is_file() or ".git" in path.parts:
            continue
        if not _is_text_file(path):
            continue
        if path.resolve() in skip:
            continue  # this file documents the pattern on purpose
        text = path.read_text(encoding="utf-8")
        if _looks_mangled(text):
            offenders.append(path.relative_to(ROOT).as_posix())
    return offenders


def test_the_mojibake_check_detects_real_corruption():
    # The positive control for the test below. A scan that never reports
    # anything is indistinguishable from a scan that is not looking, so this
    # pins that the detector fires on the actual damage: an em dash that was
    # UTF-8 encoded, decoded as cp1252, then re-saved as UTF-8. Those leading
    # "a-circumflex, euro" bytes are what broke the watcher message once.
    original = "USB device detected \u2014 switching display\n"
    damaged = original.encode("utf-8").decode("cp1252")
    assert _looks_mangled(damaged), "the detector must fire on cp1252 mojibake"

    # And that it leaves correct text and plain ASCII alone, so it cannot be
    # passing by flagging everything.
    assert not _looks_mangled(original)
    assert not _looks_mangled("plain ascii\n")
    # A BOM is the other failure this guards, reported the same way.
    assert _looks_mangled("\ufeff# heading\n")


def test_no_mojibake_in_tracked_text_files():
    # A PowerShell Get-Content/Set-Content round-trip rewrote the em dash in
    # "USB device detected - switching display" as UTF-8 bytes read back as
    # cp1252. cp1252-mojibake always starts with one of a few lead characters,
    # so match that shape rather than a literal.
    #
    # The scan reads what git tracks, because that is the set that ships: a
    # gitignored scratch file must not be able to fail the suite. When git is
    # unavailable the test skips rather than falling back to the working tree,
    # so the result says exactly what was checked either way.
    tracked = _tracked_files()
    if tracked is None:
        pytest.skip("git is not available, so the tracked file list is unknown")

    # Guards the guard: a short or empty list would let the scan pass without
    # reading the files this test exists for.
    assert tracked, "git reported no tracked files"
    assert any(path.name == "README.md" for path in tracked), "no README.md in the tracked list"

    offenders = _mangled_files(sorted(tracked), skip=[__file__])
    assert offenders == [], "mangled or BOM-prefixed tracked files"


def test_sources_are_valid_utf8_without_bom():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            offenders.append((path.name, "BOM"))
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            offenders.append((path.name, "not UTF-8"))
    assert offenders == [], "source files must be UTF-8 without BOM"


def test_watcher_message_keeps_the_em_dash():
    # The em dash is the one non-ASCII character the source genuinely needs:
    # it is what a user reads in the log. It must stay U+2014, not be escaped
    # or transliterated by a re-save.
    source = (SRC / "kvm_switcher" / "watcher.py").read_text(encoding="utf-8")
    assert "USB device detected \u2014 switching display" in source
