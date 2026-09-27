"""Tests that do not need a monitor, USB bus, or login service.

Hardware and OS integration (ddcutil, BetterDisplay, Windows VCP, systemd,
launchd, schtasks) stay manual. See README.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
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
            [("0x0f", ""), ("0x12", "")],
        )

    def test_monitor_name_match(self):
        self.assertTrue(ks.monitor_matches("MSI Monitor", "Monitor"))
        self.assertFalse(ks.monitor_matches("DELL U2720Q", "Monitor"))
        self.assertTrue(ks.monitor_matches("anything", ""))


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
    def test_bash_scripts_parse(self):
        scripts = sorted(ROOT.glob("*.sh"))
        self.assertGreaterEqual(len(scripts), 1)
        for script in scripts:
            subprocess.run(["bash", "-n", str(script)], check=True)


if __name__ == "__main__":
    unittest.main()
