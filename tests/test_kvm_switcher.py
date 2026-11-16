"""Tests that do not need a monitor, USB bus, or login service.

Hardware and OS integration (ddcutil, BetterDisplay, Windows VCP, systemd,
launchd, schtasks) stay manual. See README.
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "kvm-switcher.py"


def load_switcher():
    spec = importlib.util.spec_from_file_location("kvm_switcher", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["kvm_switcher"] = module
    spec.loader.exec_module(module)
    return module


ks = load_switcher()


class ParseConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_legacy_text_and_json(self):
        legacy = Path(self._tmp.name) / "devices.txt"
        legacy.write_text(
            "# comment\n\ninput 0x0f\ntag 42\n1234:abcd  # Receiver\n1234:0001\n",
            encoding="utf-8",
        )
        loaded = ks.parse_legacy(legacy)
        self.assertEqual(loaded.devices, [("1234:abcd", "Receiver"), ("1234:0001", "1234:0001")])
        self.assertEqual(loaded.display.input, "0x0f")
        self.assertEqual(loaded.display.tag, "42")

        path = Path(self._tmp.name) / "config.json"
        path.write_text(
            '{"devices":[{"id":"1234:ABCD","name":"Receiver"}],'
            '"display":{"name":"Monitor","bus":"5","input":"0x0f","inputLabel":"DisplayPort-1"}}\n',
            encoding="utf-8",
        )
        loaded = ks.parse_json_config(path)
        self.assertEqual(loaded.devices, [("1234:abcd", "Receiver")])
        self.assertEqual(loaded.display.bus, "5")
        self.assertEqual(loaded.display.label, "DisplayPort-1")

    def test_missing_file_is_empty(self):
        loaded = ks.load_config(Path("/no/such/config.json"))
        self.assertEqual(loaded.devices, [])
        self.assertIsNone(loaded.display)

    def test_json_falls_back_to_legacy_sibling(self):
        legacy = Path(self._tmp.name) / "devices.txt"
        legacy.write_text("1234:abcd  # Receiver\ninput 0x11\n", encoding="utf-8")
        loaded = ks.load_config(Path(self._tmp.name) / "config.json")
        self.assertEqual(loaded.devices, [("1234:abcd", "Receiver")])
        self.assertEqual(loaded.display.input, "0x11")


class InputValueTests(unittest.TestCase):
    def test_hex_and_decimal(self):
        self.assertEqual(ks.parse_input_value("0x0F"), 15)
        self.assertEqual(ks.parse_input_value("15"), 15)
        self.assertEqual(ks.vcp_text("15"), "0xf")
        self.assertEqual(ks.vcp_text("0x0f"), "0x0f")

    def test_rejects_junk(self):
        with self.assertRaises(ValueError):
            ks.parse_input_value("hdmi")

    def test_input_comes_only_from_config(self):
        self.assertIsNone(ks.effective_input(None))
        self.assertEqual(ks.effective_input("0x12"), "0x12")


class CatalogAndSaveTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_catalog_keeps_saved_devices_that_are_unplugged(self):
        saved = [("1234:abcd", "Receiver"), ("1234:0001", "Mic")]
        with patch.object(ks, "connected_devices", return_value=[("1234:abcd", "Receiver live")]):
            items = ks.catalog(saved)
        self.assertEqual(items[0], ("1234:abcd", "Receiver live"))
        self.assertEqual(items[1], ("1234:0001", "Mic (not connected)"))

    def test_save_roundtrip(self):
        path = Path(self._tmp.name) / "config.json"
        items = [("1234:abcd", "Receiver (not connected)")]
        display = ks.DisplayTarget(input="0x11", label="HDMI-1", bus="5", tag="42", name="Monitor")
        ks.save_config(path, ["1234:abcd"], items, display)
        loaded = ks.parse_json_config(path)
        self.assertEqual(loaded.devices, [("1234:abcd", "Receiver")])
        self.assertEqual(loaded.display.input, "0x11")
        self.assertEqual(loaded.display.bus, "5")
        self.assertEqual(loaded.display.tag, "42")
        self.assertEqual(loaded.display.label, "HDMI-1")


class DeviceListingTests(unittest.TestCase):
    def test_lsusb_skips_root_hubs_and_duplicates(self):
        output = (
            "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\n"
            "Bus 001 Device 004: ID 1234:ABCD USB Receiver\n"
            "Bus 002 Device 003: ID 1234:abcd USB Receiver\n"
            "noise\n"
        )
        self.assertEqual(ks.parse_lsusb(output), [("1234:abcd", "USB Receiver")])

    def test_ioreg_pairs_vendor_and_product(self):
        output = """
+-o AppleUSBXHCI Root Hub@0
  "idVendor" = 0x1d6b
  "idProduct" = 0x0002
+-o Camera@1
  "idVendor" = 0x1234
  "idProduct" = 0x2
"""
        found = ks.parse_ioreg(output)
        self.assertEqual(found, [("1d6b:0002", "AppleUSBXHCI Root Hub"), ("1234:0002", "Camera")])

    def test_windows_lines(self):
        output = "1234:abcd\tUSB Receiver\nnot-a-device\n1234:abcd\tagain\n"
        self.assertEqual(ks.parse_windows_devices(output), [("1234:abcd", "USB Receiver")])

    def test_windows_parses_literal_backtick_t(self):
        # Regression: PowerShell only expands `t inside a double-quoted format
        # string. A single-quoted one emits backtick+t literally, and requiring a
        # real tab there made every device parse away, so the watcher never fired.
        output = "1234:abcd`tUSB Receiver\n5678:ef01`tKeyboard\n"
        self.assertEqual(
            ks.parse_windows_devices(output),
            [("1234:abcd", "USB Receiver"), ("5678:ef01", "Keyboard")],
        )

    def test_windows_script_format_string_is_double_quoted(self):
        script = ks.windows_device_script()
        # The tab lives in the output format string, which must be double-quoted:
        # PowerShell only expands `t there. A single-quoted one emitted "`t".
        self.assertIn('"{0}`t{1}"', script)
        self.assertNotIn("\"{0}:{1}`t", script)
        # The product string is what makes a device identifiable; the old query
        # only read FriendlyName and the USB class name.
        self.assertIn("BusReportedDeviceDesc", script)
        self.assertNotIn("-notmatch '&MI_'", script)

    def test_generic_names_are_not_useful(self):
        for name in (
            "USB Composite Device",
            "USB Input Device",
            "Generic USB Hub",
            "usb composite device",
            "",
            "   ",
        ):
            self.assertFalse(ks.useful_device_name(name), name)
        for name in ("Razer Seiren V3 Mini", "8BitDo Retro 18 Numpad", "USB2.1 Hub"):
            self.assertTrue(ks.useful_device_name(name), name)


class DisplayDiscoveryTests(unittest.TestCase):
    def test_detect_skips_invalid_and_reads_model(self):
        output = """
Invalid display
   I2C bus:  /dev/i2c-3
   Model:                DELL U2720Q

Display 1
   I2C bus:  /dev/i2c-5
   EDID synopsis:
      Model:                Monitor
"""
        found = ks.parse_ddcutil_detect(output)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].bus, "5")
        self.assertEqual(found[0].name, "Monitor")

    def test_capabilities_and_mccs(self):
        caps = """
  Feature: 60 (Input Source)
      Values:
         0f: DisplayPort-1
         11: HDMI-1
  Feature: 62 (Audio)
"""
        self.assertEqual(
            ks.parse_input_sources(caps),
            [("0x0f", "DisplayPort-1"), ("0x11", "HDMI-1")],
        )
        self.assertEqual(
            ks.parse_input_sources("vcp(10 60(0F 12) )"),
            [("0x0f", "DisplayPort-1"), ("0x12", "HDMI-2")],
        )

    def test_windows_capability_string_gives_labelled_inputs(self):
        # The exact shape a monitor returns through DXVA2 capabilities.
        caps = (
            "(prot(monitor)type(lcd)model(FALCON)cmds(01 02 03 07 0C 4E F3 E3)"
            "vcp(02 04 05 08 0B 0C 10 12 60(11 12 0F 10) 62 8D FF)mswhql(1)mccs_ver(2.0))"
        )
        self.assertEqual(
            ks.parse_mccs_inputs(caps),
            [
                ("0x11", "HDMI-1"),
                ("0x12", "HDMI-2"),
                ("0x0f", "DisplayPort-1"),
                ("0x10", "DisplayPort-2"),
            ],
        )

    def test_unknown_input_code_still_listed(self):
        # A monitor may advertise a value the table does not know; keep the code
        # rather than dropping the choice.
        self.assertEqual(ks.parse_mccs_inputs("vcp(60(11 7a))"), [("0x11", "HDMI-1"), ("0x7a", "")])

    def test_input_labels(self):
        self.assertEqual(ks.input_label("0x0f"), "DisplayPort-1")
        self.assertEqual(ks.input_label("0F"), "DisplayPort-1")
        self.assertEqual(ks.input_label("12"), "HDMI-2")
        self.assertEqual(ks.input_label("0x99"), "")

    def test_monitor_name_match(self):
        self.assertTrue(ks.monitor_matches("MSI Monitor", "Monitor"))
        self.assertFalse(ks.monitor_matches("DELL U2720Q", "Monitor"))
        self.assertTrue(ks.monitor_matches("anything", ""))

    def test_windows_generic_monitor_still_matches(self):
        # Regression: Windows reports many panels as "Generic PnP Monitor", so a
        # saved real model name never matched and switch exited with no monitor.
        self.assertTrue(ks.windows_monitor_matches("Generic PnP Monitor", "MEG 342C OLED"))
        self.assertTrue(ks.windows_monitor_matches("MEG 342C OLED", "MEG 342C OLED"))
        # A genuinely different, identifiable monitor must still be skipped.
        self.assertFalse(ks.windows_monitor_matches("DELL U2720Q", "MEG 342C OLED"))
        self.assertTrue(ks.windows_monitor_matches("DELL U2720Q", ""))


class WindowsSwitchTargetTests(unittest.TestCase):
    """The generic-name fallback must not switch monitors it cannot identify."""

    class _FakeDxva2:
        def __init__(self, ok=True):
            self.written = []
            self._ok = ok

        def SetVCPFeature(self, handle, code, value):
            self.written.append(handle)
            return self._ok

    def _run(self, descriptions, name, ok=True):
        dxva2 = self._FakeDxva2(ok)
        collected = [(d, f"h{i}", None, 1) for i, d in enumerate(descriptions)]
        with patch.object(ks, "_windows_physical_monitors", return_value=(dxva2, collected)), \
             patch.object(ks, "_release_windows_monitors"):
            return ks._switch_windows(0x0F, name), dxva2.written

    def test_single_generic_monitor_is_switched(self):
        switched, written = self._run(["Generic PnP Monitor"], "MEG 342C OLED")
        self.assertEqual(switched, 1)
        self.assertEqual(written, ["h0"])

    def test_two_monitors_with_unmatched_name_switches_nothing(self):
        # Two displays, neither carrying the saved name: writing to either could
        # black out the wrong screen, so nothing may be touched.
        switched, written = self._run(
            ["Generic PnP Monitor", "Generic PnP Monitor"], "MEG 342C OLED"
        )
        self.assertEqual(switched, 0)
        self.assertEqual(written, [])

    def test_named_match_still_switches_among_several(self):
        switched, written = self._run(
            ["DELL U2720Q", "MEG 342C OLED"], "MEG 342C OLED"
        )
        self.assertEqual(switched, 1)
        self.assertEqual(written, ["h1"])

    def test_failed_write_is_not_counted(self):
        switched, _ = self._run(["Generic PnP Monitor"], "", ok=False)
        self.assertEqual(switched, 0)


class WatcherProcessTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pid_file = Path(self._tmp.name) / "watcher.pid"

    def test_pid_file_roundtrip(self):
        with patch.object(ks, "watcher_pid_path", return_value=self.pid_file):
            ks.write_watcher_pid(4321)
            self.assertEqual(ks.read_watcher_pid(), 4321)

    def test_missing_or_junk_pid_file_is_none(self):
        with patch.object(ks, "watcher_pid_path", return_value=self.pid_file):
            self.assertIsNone(ks.read_watcher_pid())
            self.pid_file.write_text("not-a-number", encoding="utf-8")
            self.assertIsNone(ks.read_watcher_pid())

    def test_dead_process_is_not_alive(self):
        # A pid that cannot be running: the file is removed, so OpenProcess fails.
        self.assertFalse(ks.process_alive(0))
        self.assertFalse(ks.process_alive(-1))

    def test_stop_clears_stale_pid_file(self):
        # Regression: uninstall claimed success while the watcher kept running.
        with patch.object(ks, "watcher_pid_path", return_value=self.pid_file):
            ks.write_watcher_pid(999999)
            self.assertFalse(ks.stop_watcher_process())
            self.assertFalse(self.pid_file.exists())

    def test_stop_refuses_a_recycled_pid(self):
        # A pid file left behind by a crash must not let uninstall kill whatever
        # unrelated process later inherits that pid.
        import subprocess as sp
        victim = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (victim.kill(), victim.wait()))
        self.pid_file.write_text(f"{victim.pid}\nnot-our-start-time\n", encoding="utf-8")
        with patch.object(ks, "watcher_pid_path", return_value=self.pid_file):
            self.assertFalse(ks.stop_watcher_process())
        self.assertIsNone(victim.poll(), "an unrelated process was killed")


class SwitchCommandTests(unittest.TestCase):
    def test_linux_and_darwin_argv(self):
        self.assertEqual(ks.linux_switch_argv("0x0f"), ["ddcutil", "setvcp", "60", "0x0f"])
        self.assertEqual(
            ks.linux_switch_argv("0x0f", "5"),
            ["ddcutil", "--bus", "5", "setvcp", "60", "0x0f"],
        )
        self.assertEqual(
            ks.darwin_switch_argv("17", "42", "/opt/homebrew/bin/betterdisplaycli"),
            [
                "/opt/homebrew/bin/betterdisplaycli", "set",
                "--tagID=42",
                "--feature=ddc",
                "--vcp=inputSelect",
                "--value=0x11",
            ],
        )

    def test_switch_requires_a_configured_input(self):
        with patch.object(ks, "load_config", return_value=ks.Config()), \
             self.assertRaises(SystemExit) as caught:
            ks.switch_monitor()
        self.assertIn("No monitor input configured", str(caught.exception))

    def test_switch_uses_built_command(self):
        saved = ks.Config(display=ks.DisplayTarget(input="0x0f", bus="5", name="Monitor"))
        with patch.object(ks, "platform_key", return_value="linux"), \
             patch.object(ks, "load_config", return_value=saved), \
             patch.object(ks.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            code = ks.switch_monitor("0x10")
        self.assertEqual(code, 0)
        run.assert_called_once_with(["ddcutil", "--bus", "5", "setvcp", "60", "0x10"])

    def test_presence_is_any_match(self):
        with patch.object(ks, "connected_devices", return_value=[("1234:abcd", "Receiver")]):
            self.assertTrue(ks.devices_present(["1234:0001", "1234:abcd"]))
            self.assertFalse(ks.devices_present(["1234:0001"]))
            self.assertFalse(ks.devices_present([]))


class StatusTests(unittest.TestCase):
    def test_format_reports_choices_and_watcher(self):
        config = ks.Config(
            devices=[("1234:abcd", "Receiver")],
            display=ks.DisplayTarget(input="0x0f", label="DisplayPort-1", bus="5", name="Monitor"),
        )
        text = ks.format_status(config, installed=True, running=True)
        self.assertIn("1234:abcd  Receiver", text)
        self.assertIn("Display: 0x0f DisplayPort-1 on Monitor, bus 5", text)
        self.assertIn("Watcher: running", text)
        self.assertIn("installed, not running", ks.format_status(config, True, False))
        self.assertIn("not installed", ks.format_status(ks.Config(), False, False))
        self.assertIn("Not configured", ks.format_status(ks.Config(), False, False))

    def test_running_parsers(self):
        self.assertTrue(ks.parse_systemctl_is_active("active\n"))
        self.assertFalse(ks.parse_systemctl_is_active("inactive\n"))
        listed = "123\t0\tcom.kvm-switcher.watcher\n"
        self.assertTrue(ks.parse_launchctl_list(listed))
        self.assertFalse(ks.parse_launchctl_list("-\t0\tcom.kvm-switcher.watcher\n"))
        self.assertTrue(ks.parse_schtasks_status("Status:    Running\n"))
        self.assertFalse(ks.parse_schtasks_status("Status:    Ready\n"))


class CliTests(unittest.TestCase):
    def test_unknown_command(self):
        with self.assertRaises(SystemExit) as caught:
            ks.main(["kvm-switcher.py", "nope"])
        self.assertIn("Unknown command", str(caught.exception))

    def test_version_prints_constant(self):
        with patch("builtins.print") as printed:
            ks.main(["kvm-switcher.py", "version"])
        printed.assert_called_once_with(ks.VERSION)
        self.assertRegex(ks.VERSION, r"^\d+\.\d+\.\d+$")

    def test_help_prints_doc(self):
        with patch("builtins.print") as printed:
            ks.main(["kvm-switcher.py", "--help"])
        printed.assert_called_once_with(ks.__doc__)


class ShellSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_scripts_parse(self):
        scripts = sorted(ROOT.glob("*.sh"))
        self.assertGreaterEqual(len(scripts), 1)
        for script in scripts:
            subprocess.run(["bash", "-n", str(script)], check=True)


class PortabilityTests(unittest.TestCase):
    """The module has to import on every OS, not just the one running the tests."""

    def test_import_survives_without_windows_ctypes_symbols(self):
        # ctypes.WINFUNCTYPE/WinDLL exist only on Windows. Binding one at module
        # level made `import kvm-switcher` raise AttributeError on Linux and
        # macOS, which broke every non-Windows install.
        code = (
            "import ctypes, importlib.util, sys\n"
            "for name in ('WINFUNCTYPE', 'WinDLL', 'windll'):\n"
            "    if hasattr(ctypes, name):\n"
            "        delattr(ctypes, name)\n"
            f"spec = importlib.util.spec_from_file_location('ks_probe', {str(SCRIPT)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules['ks_probe'] = module\n"
            "spec.loader.exec_module(module)\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=90
        )
        self.assertEqual(
            result.returncode, 0,
            f"module failed to import without Windows ctypes symbols:\n{result.stderr}",
        )

    def test_no_windows_binding_at_module_level(self):
        # Inside a function is fine (it only runs on Windows); at module scope it
        # runs on every import. Only inspect top-level expressions, not bodies.
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        offenders = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # bodies only execute when called
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr in {"WinDLL", "WINFUNCTYPE", "windll"}:
                    offenders.append((sub.attr, getattr(sub, "lineno", 0)))
        self.assertEqual(offenders, [], "Windows-only ctypes call at module scope")


class WatchLoopTests(unittest.TestCase):
    def test_switch_is_retried_only_after_the_device_goes_away(self):
        """A failed or repeated poll must not re-switch while the device stays."""
        calls = []
        sequence = [True, True, False, True, True]
        with patch.object(ks, "load_config", return_value=ks.Config(devices=[("1234:abcd", "Dev")])), \
             patch.object(ks, "devices_present", side_effect=sequence), \
             patch.object(ks, "switch_monitor", side_effect=lambda *a: calls.append(1)), \
             patch.object(ks, "time", **{"sleep": lambda _s: None}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StopIteration):
                ks._watch_loop()
        # True,True (one switch) then False then True,True (second switch)
        self.assertEqual(len(calls), 2)

    def test_failed_switch_does_not_retry_every_poll(self):
        """If switching raises, the flag must stay set so the screen does not
        flash once per poll."""
        calls = []
        sequence = [True, True, True]

        def boom(*_a):
            calls.append(1)
            raise OSError("ddc failed")

        with patch.object(ks, "load_config", return_value=ks.Config(devices=[("1234:abcd", "Dev")])), \
             patch.object(ks, "devices_present", side_effect=sequence), \
             patch.object(ks, "switch_monitor", side_effect=boom), \
             patch.object(ks, "time", **{"sleep": lambda _s: None}), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(StopIteration):
                ks._watch_loop()
        self.assertEqual(len(calls), 1, "switch retried while the device stayed present")

    def test_second_watcher_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "watcher.pid"
            pid_file.write_text("4242\n", encoding="utf-8")
            with patch.object(ks, "watcher_pid_path", return_value=pid_file), \
                 patch.object(ks, "process_alive", return_value=True), \
                 patch.object(ks, "ensure_config"), \
                 patch.object(ks, "maybe_install"), \
                 patch.object(ks, "os") as fake_os:
                fake_os.getpid.return_value = 999
                with self.assertRaises(SystemExit) as caught:
                    ks.watch()
            self.assertIn("already running", str(caught.exception))


class UpdateTests(unittest.TestCase):
    def test_version_comparison(self):
        cases = [
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
        ]
        for candidate, current, expected in cases:
            with self.subTest(candidate=candidate, current=current):
                self.assertEqual(ks.version_is_newer(candidate, current), expected)

    def test_download_is_rejected_when_not_newer(self):
        body = b'#!/usr/bin/env python3\nVERSION = "0.0.1"\n'
        with self.assertRaises(SystemExit) as caught:
            ks._verified_program(body, "0.1.6")
        self.assertIn("not newer", str(caught.exception))

    def test_download_is_rejected_when_it_does_not_compile(self):
        body = b'VERSION = "9.9.9"\ndef broken(:\n'
        with self.assertRaises(SystemExit) as caught:
            ks._verified_program(body, "0.1.6")
        self.assertIn("does not compile", str(caught.exception))

    def test_download_is_rejected_when_it_is_not_the_program(self):
        with self.assertRaises(SystemExit) as caught:
            ks._verified_program(b"<html>404</html>", "0.1.6")
        self.assertIn("not kvm-switcher.py", str(caught.exception))

    def test_download_is_rejected_when_it_has_a_bom(self):
        # A BOM breaks the shebang that install.sh symlinks onto PATH.
        body = b'\xef\xbb\xbfVERSION = "9.9.9"\n'
        with self.assertRaises(SystemExit) as caught:
            ks._verified_program(body, "0.1.6")
        self.assertIn("BOM", str(caught.exception))

    def test_good_download_is_accepted(self):
        body = b'#!/usr/bin/env python3\nVERSION = "9.9.9"\n'
        self.assertEqual(ks._verified_program(body, "0.1.6"), body)

    def test_checkout_refuses_to_self_update(self):
        # A git checkout must be updated with git, never overwritten in place.
        with patch.object(ks, "running_from_checkout", return_value=True), \
             patch.object(ks, "published_version", return_value="9.9.9"):
            with self.assertRaises(SystemExit) as caught:
                ks.update()
        self.assertIn("git pull", str(caught.exception))

    def test_same_program_matches_quoted_and_absolute_paths(self):
        target = str(SCRIPT)
        self.assertTrue(ks.same_program(target, target))
        self.assertTrue(ks.same_program(f'"{target}"', target))
        self.assertTrue(ks.same_program(f"python.exe {target}", target))
        self.assertFalse(ks.same_program("/somewhere/else/kvm-switcher.py", target))
        self.assertFalse(ks.same_program("", target))

    def test_update_leaves_other_watchers_alone(self):
        """A copy installed elsewhere must not stop a watcher it does not own."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "kvm-switcher.py"
            target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
            body = b'VERSION = "9.9.9"\n'
            with patch.object(ks, "running_from_checkout", return_value=False), \
                 patch.object(ks, "published_version", return_value="9.9.9"), \
                 patch.object(ks, "_download_program", return_value=body), \
                 patch.object(ks, "read_watcher_pid", return_value=4242), \
                 patch.object(ks, "process_alive", return_value=True), \
                 patch.object(ks, "running_watcher_target", return_value="/other/install/kvm-switcher.py"), \
                 patch.object(ks, "__file__", str(target)), \
                 patch.object(ks, "stop_watcher_process") as stop, \
                 patch.object(ks, "spawn_watcher") as spawn, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = ks.update()
            written = target.read_bytes()
        self.assertEqual(code, 0)
        stop.assert_not_called()   # someone else's watcher keeps running
        spawn.assert_not_called()
        self.assertIn(b"9.9.9", written)

    def test_watcher_spawn_does_not_inherit_stdio(self):
        # An inherited pipe made `update` hang when its output was captured.
        with patch.object(ks.subprocess, "Popen") as popen:
            popen.return_value.pid = 1234
            self.assertEqual(ks.spawn_watcher(Path("program.py")), 1234)
        _args, kwargs = popen.call_args
        self.assertEqual(kwargs["stdout"], ks.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], ks.subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], ks.subprocess.DEVNULL)


class EncodingTests(unittest.TestCase):
    """The source contains a non-ASCII em dash; re-saving it must not mangle it."""

    def test_no_mojibake_in_tracked_text_files(self):
        # A PowerShell Get-Content/Set-Content round-trip rewrote the em dash in
        # "USB device detected — switching display" as UTF-8 bytes read back as
        # cp1252, leaving "â" + euro + smart-quote. cp1252-mojibake always starts
        # with one of a few lead characters, so match that shape rather than a
        # literal (a literal would also match this test's own source).
        mojibake = re.compile("[\u00c2\u00c3\u00e2][\u0080-\u00ff\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc\u2013-\u203a\u20ac\u2122\ufffd]")

        def looks_mangled(text: str) -> bool:
            if text.startswith("\ufeff"):
                return True
            return bool(mojibake.search(text))

        offenders = []
        # Scan what git tracks, not the working directory: a local scratch file
        # that is gitignored must not fail the suite.
        tracked = None
        try:
            listing = subprocess.run(
                ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=60
            )
            if listing.returncode == 0:
                tracked = [
                    ROOT / name
                    for name in listing.stdout.decode("utf-8", "replace").split("\0")
                    if name
                ]
        except (OSError, subprocess.SubprocessError):
            tracked = None
        paths = tracked if tracked is not None else sorted(ROOT.rglob("*"))
        for path in paths:
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix.lower() not in {".py", ".md", ".ps1", ".sh", ".json", ".yml"}:
                continue
            if path == Path(__file__).resolve():
                continue  # this file documents the pattern on purpose
            text = path.read_text(encoding="utf-8")
            if looks_mangled(text):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "mangled or BOM-prefixed files")

    def test_source_is_valid_utf8_without_bom(self):
        raw = SCRIPT.read_bytes()
        self.assertFalse(
            raw.startswith(b"\xef\xbb\xbf"),
            "kvm-switcher.py must not start with a BOM: its shebang is executed directly",
        )
        raw.decode("utf-8")  # raises if the file is not valid UTF-8


if __name__ == "__main__":
    unittest.main()
