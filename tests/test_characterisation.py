"""Behaviour-locking tests for the single-file program.

These are characterisation tests, not unit tests. Every expected value was
recorded from the real program, and the contract is that this file passes
UNCHANGED after the package split: if an assertion here has to be edited, the
refactor changed observable behaviour.

The device ids, product names, MCCS capability string and display name used
below are synthetic fixtures rather than one contributor's hardware. They are
deliberately fake ("aa01:xxxx" ids, invented product strings): these tests lock
down parsing and formatting rules, so what the fixtures contain only has to be
shaped like real output, never real.

So the assertions go through entry points that will still exist (main(argv), a
config file on disk, stdout) rather than internals. The few private names the
Windows switch path needs are reached through api() and replace(), which are the
only places meant to be edited when the code moves.

Nothing here writes to a monitor, starts a watcher, or touches the login service.
The one test that reads the real USB bus is opt-in: KVM_CHARACTERISATION_LIVE=1.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import json
import os
import pkgutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "kvm-switcher.py"


# --------------------------------------------------------------------------
# Loading the program. This section is the only one the split should touch.
# --------------------------------------------------------------------------


def load_program():
    """Import the program under test.

    Same idiom as tests/test_kvm_switcher.py: while the program is a single file
    it is loaded by path. After the split the file is gone, so the installed
    package is imported instead; that is the only change this migration needs,
    and no assertion below moves. Once `src/` exists the second branch takes over
    on its own, so the tests keep running whichever layout is present.
    """
    if PROGRAM.is_file():
        spec = importlib.util.spec_from_file_location("kvm_switcher", PROGRAM)
        module = importlib.util.module_from_spec(spec)
        sys.modules["kvm_switcher"] = module
        spec.loader.exec_module(module)
        return module
    source = ROOT / "src"
    if source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    return importlib.import_module("kvm_switcher")


program = load_program()


def modules():
    """Every module the split may put a name in.

    Today that is one file. After the split the program is a package, so this
    imports its submodules rather than guessing a layout: a name may end up in
    mccs.py, devices/windows.py, displays/windows.py and so on, and the tests
    must not care which. A submodule that cannot be imported on this platform is
    skipped, and lookup then simply does not see it.
    """
    found = [program]
    package_path = getattr(program, "__path__", None)
    if package_path is not None:
        for info in pkgutil.walk_packages(package_path, program.__name__ + "."):
            try:
                found.append(importlib.import_module(info.name))
            except Exception:  # noqa: BLE001 - a foreign-platform module may refuse
                continue
    return found


def api(*names):
    """The program's callable called one of `names`, wherever it lives.

    Mostly these are the names the split is asked to preserve (format_status,
    parse_mccs_inputs, connected_devices, monitor_matches, version_is_newer).
    A few live in per-OS modules the split creates (parse_windows_devices,
    useful_device_name, windows_monitor_matches), which is why lookup is by
    name across modules() instead of by attribute on one object. Extra names are
    spellings the split may reasonably choose, tried in order.
    """
    for name in names:
        for owner in modules():
            found = getattr(owner, name, None)
            if callable(found):
                return found
    raise AttributeError(f"the program no longer exposes a callable named {names[0]!r}")


@contextlib.contextmanager
def replace(replacement, *names):
    """Temporarily replace the monitor-enumeration hooks used by the switch path.

    After the split a name is defined in one module but called from another, so
    patching a single object could miss the call site. Replacing it everywhere
    it is visible works whichever module ends up calling it, and needs no
    knowledge of the layout. Alternate spellings can be passed and the first one
    the program actually exposes is used.
    """
    patchers = []
    for name in names:
        patchers = [patch.object(owner, name, replacement)
                    for owner in modules() if hasattr(owner, name)]
        if patchers:
            break
    if not patchers:
        raise AttributeError(f"the program no longer exposes {names[0]!r}")
    for patcher in patchers:
        patcher.start()
    try:
        yield
    finally:
        for patcher in reversed(patchers):
            patcher.stop()


load_config = api("load_config")
format_status = api("format_status")
parse_mccs_inputs = api("parse_mccs_inputs")
input_label = api("input_label")
monitor_matches = api("monitor_matches")
windows_monitor_matches = api("windows_monitor_matches")
useful_device_name = api("useful_device_name")
parse_windows_devices = api("parse_windows_devices")
version_is_newer = api("version_is_newer")
connected_devices = api("connected_devices")
main = api("main")


# --------------------------------------------------------------------------
# Synthetic fixtures.
# --------------------------------------------------------------------------

# 11 invented USB devices in scan order, shaped like what a Windows scan
# reports but with reserved-looking "aa01" ids so nothing here identifies real
# hardware. Between them they cover the cases the scan and the status block
# have to survive: hubs, a composite multi-function device, product strings that
# look like vendor branding, punctuation ("+", "(", ")", "."), a very long name,
# and a single short token.
SAMPLE_DEVICES = [
    ("aa01:0001", "Fiction USB2.1 Hub"),
    ("aa01:0002", "Fiction StreamCam 4K"),
    ("aa01:0003", "Fiction Multi-Port Composite Docking Station with Ethernet"),
    ("aa01:0004", "Fiction USB2.0 Audio"),
    ("aa01:0005", "Fiction Microphone+Mixer (Gen 2)"),
    ("aa01:0006", "Fiction Retro 18 Keypad"),
    ("aa01:0007", "Fiction Pointer 8+ADV"),
    ("aa01:0008", "Beacon"),
    ("aa01:0009", "Fiction Bluetooth Adapter"),
    ("aa01:000a", "Fiction USB3.2 Hub"),
    ("aa01:000b", "Fiction LED Controller"),
]

# A synthetic MCCS capability string with the same structure a monitor answers
# with (prot/type/model/cmds/vcp/mswhql/mccs_ver). The input list it advertises
# is 60(11 12 0F 10), the four standard codes for HDMI-1, HDMI-2, DisplayPort-1
# and DisplayPort-2.
SAMPLE_CAPABILITIES = (
    "(prot(monitor)type(lcd)model(SAMPLE)cmds(01 02 03 07 0C 4E F3 E3)"
    "vcp(02 04 05 08 0B 0C 10 12 60(11 12 0F 10) 62 8D FF)mswhql(1)mccs_ver(2.0))"
)

# Not a generic Windows placeholder ("Generic PnP Monitor" and friends) and not
# a substring of one, so the one-monitor rule keeps exercising both branches.
SAMPLE_DISPLAY_NAME = "Test Monitor 4K"

# A saved display block as `configure` writes it on Windows.
SAVED_DISPLAY = {
    "name": SAMPLE_DISPLAY_NAME,
    "input": "0x12",
    "inputLabel": "HDMI-2",
}


def config_file(devices=(), display=None):
    """A config object built by the program's own JSON reader.

    Constructing the dataclasses directly would pin their location in the
    package; going through the file format keeps this test about behaviour.
    """
    payload = {
        "devices": [{"id": vidpid, "name": name} for vidpid, name in devices],
    }
    if display is not None:
        payload["display"] = display
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return load_config(path)


def device_lines(devices):
    """The Devices: block format_status emits, built from the fixture pairs."""
    return ["Devices:"] + [f"  {vidpid}  {name}" for vidpid, name in devices]


# --------------------------------------------------------------------------
# MCCS input selection
# --------------------------------------------------------------------------


class MccsInputTests(unittest.TestCase):
    """The four inputs the synthetic capability string advertises."""

    def test_capability_string_maps_to_four_labelled_inputs(self):
        self.assertEqual(
            parse_mccs_inputs(SAMPLE_CAPABILITIES),
            [
                ("0x11", "HDMI-1"),
                ("0x12", "HDMI-2"),
                ("0x0f", "DisplayPort-1"),
                ("0x10", "DisplayPort-2"),
            ],
        )

    def test_order_follows_the_capability_string(self):
        # The picker shows inputs in the order the monitor lists them, so a
        # different order in the string must give a different order out.
        self.assertEqual(
            parse_mccs_inputs("vcp(60(10 0F 12 11))"),
            [
                ("0x10", "DisplayPort-2"),
                ("0x0f", "DisplayPort-1"),
                ("0x12", "HDMI-2"),
                ("0x11", "HDMI-1"),
            ],
        )

    def test_codes_are_lowercase_two_digit_hex(self):
        # Uppercase in the string, canonical lowercase in the config and argv.
        codes = [code for code, _label in parse_mccs_inputs(SAMPLE_CAPABILITIES)]
        self.assertEqual(codes, ["0x11", "0x12", "0x0f", "0x10"])
        for code in codes:
            self.assertRegex(code, r"^0x[0-9a-f]{2}$")

    def test_unknown_value_is_kept_without_a_label(self):
        # An advertised value the table does not know stays selectable rather
        # than being dropped, with an empty label.
        self.assertEqual(
            parse_mccs_inputs("vcp(60(11 7a))"), [("0x11", "HDMI-1"), ("0x7a", "")]
        )

    def test_no_input_feature_yields_no_choices(self):
        self.assertEqual(parse_mccs_inputs("(prot(monitor)vcp(02 04 62 8D FF))"), [])
        self.assertEqual(parse_mccs_inputs(""), [])

    def test_input_labels_for_the_standard_input_codes(self):
        self.assertEqual(input_label("0x11"), "HDMI-1")
        self.assertEqual(input_label("0x12"), "HDMI-2")
        self.assertEqual(input_label("0x0f"), "DisplayPort-1")
        self.assertEqual(input_label("0x10"), "DisplayPort-2")
        # Format-insensitive, and unknown codes have no label.
        self.assertEqual(input_label("0F"), "DisplayPort-1")
        self.assertEqual(input_label("12"), "HDMI-2")
        self.assertEqual(input_label("0x99"), "")


# --------------------------------------------------------------------------
# Device listing
# --------------------------------------------------------------------------


class DeviceNameTests(unittest.TestCase):
    """All 11 fixtures survive the scan, and class names are not names."""

    def test_fixture_devices_parse_unchanged(self):
        # Both separator spellings the Windows script can emit: a real tab, and
        # the literal backtick-t an older single-quoted format string produced.
        tab_form = "".join(f"{vidpid}\t{name}\n" for vidpid, name in SAMPLE_DEVICES)
        literal_form = "".join(f"{vidpid}`t{name}\n" for vidpid, name in SAMPLE_DEVICES)
        for label, output in (("tab", tab_form), ("backtick", literal_form)):
            with self.subTest(separator=label):
                self.assertEqual(parse_windows_devices(output), SAMPLE_DEVICES)

    def test_fixture_names_all_count_as_useful(self):
        # Not one of the 11 may be mistaken for a generic USB class name; if one
        # were, it could never be identified in the picker.
        for vidpid, name in SAMPLE_DEVICES:
            with self.subTest(device=vidpid, name=name):
                self.assertTrue(useful_device_name(name))

    def test_generic_windows_class_names_are_rejected(self):
        for name in ("USB Composite Device", "Generic USB Hub"):
            with self.subTest(name=name):
                self.assertFalse(useful_device_name(name))

    def test_generic_names_are_rejected_regardless_of_case_or_padding(self):
        # The usefulness check is a case-insensitive whole-name comparison after
        # trimming, so a padded or re-cased class name is still not a name.
        for name in (
            "usb composite device",
            "USB COMPOSITE DEVICE",
            "  USB Composite Device  ",
            "generic usb hub",
            "Generic Usb Hub",
            "",
            "   ",
        ):
            with self.subTest(name=name):
                self.assertFalse(useful_device_name(name))

    @unittest.skipUnless(
        os.environ.get("KVM_CHARACTERISATION_LIVE") == "1",
        "set KVM_CHARACTERISATION_LIVE=1 to read the real USB bus",
    )
    def test_live_scan_matches_the_fixture_names(self):
        # Read-only: enumerates present USB devices, no DDC traffic. Each fixture
        # id that happens to be present must report the same name.
        live = dict(connected_devices())
        for vidpid, name in SAMPLE_DEVICES:
            if vidpid in live:
                with self.subTest(device=vidpid):
                    self.assertEqual(live[vidpid], name)
        for vidpid, name in live.items():
            with self.subTest(device=vidpid):
                self.assertTrue(useful_device_name(name), "generic name leaked through")


# --------------------------------------------------------------------------
# status text
# --------------------------------------------------------------------------


class StatusOutputTests(unittest.TestCase):
    """The exact lines a user reads from `status`."""

    def test_configured_status_is_exact(self):
        text = format_status(
            config_file(devices=SAMPLE_DEVICES, display=SAVED_DISPLAY),
            installed=True,
            running=False,
        )
        expected = "\n".join(
            device_lines(SAMPLE_DEVICES)
            + [
                f"Display: 0x12 HDMI-2 on {SAMPLE_DISPLAY_NAME}",
                "Watcher: installed, not running",
            ]
        )
        self.assertEqual(text, expected)

    def test_unconfigured_status_is_exact(self):
        # This line names the program as it is invoked today; a delivery-mechanism
        # change (zipapp) may legitimately reword it, a refactor may not.
        self.assertEqual(
            format_status(config_file(), installed=False, running=False).splitlines(),
            ["Not configured. Run: kvm-switcher configure", "Watcher: not installed"],
        )

    def test_watcher_line_has_three_states(self):
        configured = config_file(devices=SAMPLE_DEVICES, display=SAVED_DISPLAY)
        cases = {
            (False, False): "Watcher: not installed",
            (True, False): "Watcher: installed, not running",
            (False, True): "Watcher: running",
            (True, True): "Watcher: running",
        }
        for (installed, running), expected in cases.items():
            with self.subTest(installed=installed, running=running):
                self.assertEqual(
                    format_status(configured, installed, running).splitlines()[-1],
                    expected,
                )

    def test_display_line_reports_bus_and_tag(self):
        # The ddcutil (bus) and BetterDisplay (tag) hints are shown when saved.
        text = format_status(
            config_file(
                devices=[("1234:abcd", "Receiver")],
                display={
                    "name": "Monitor",
                    "bus": "5",
                    "tag": "42",
                    "input": "0x0f",
                    "inputLabel": "DisplayPort-1",
                },
            ),
            installed=True,
            running=True,
        )
        self.assertIn("Display: 0x0f DisplayPort-1 on Monitor, bus 5, tag 42", text)
        self.assertEqual(text.splitlines()[-1], "Watcher: running")

    def test_unselected_device_and_display_placeholders(self):
        # A display with no saved devices, and devices with no saved display.
        with_display = format_status(
            config_file(display=SAVED_DISPLAY), installed=False, running=False
        ).splitlines()
        self.assertEqual(
            with_display,
            [
                "Devices:",
                "  (none)",
                f"Display: 0x12 HDMI-2 on {SAMPLE_DISPLAY_NAME}",
                "Watcher: not installed",
            ],
        )
        with_devices = format_status(
            config_file(devices=SAMPLE_DEVICES[:2]), installed=False, running=False
        ).splitlines()
        self.assertEqual(
            with_devices,
            device_lines(SAMPLE_DEVICES[:2]) + ["Display: not chosen", "Watcher: not installed"],
        )


# --------------------------------------------------------------------------
# The one-monitor rule
# --------------------------------------------------------------------------


class _FakeDxva2:
    """Stands in for dxva2.dll: records writes, never talks to hardware."""

    def __init__(self, ok=True):
        self.writes = []
        self._ok = ok

    def SetVCPFeature(self, handle, code, value):
        self.writes.append((handle, code, value))
        return self._ok


def switch_windows(descriptions, saved_name, ok=True, value=0x12):
    """Run the Windows switch path over a fake monitor list."""
    switch = api("_switch_windows")
    dxva2 = _FakeDxva2(ok)
    collected = [
        (description, f"handle{index}", object(), 1)
        for index, description in enumerate(descriptions)
    ]
    with replace(lambda: (dxva2, collected), "_windows_physical_monitors"), \
            replace(lambda *_a: None, "_release_windows_monitors"):
        return switch(value, saved_name), dxva2.writes


class OneMonitorRuleTests(unittest.TestCase):
    """A generic description may only be switched when it is the only monitor."""

    def test_generic_description_alone_never_matches_a_saved_model(self):
        # The rule exists because of exactly this pair of answers.
        self.assertFalse(monitor_matches("Generic PnP Monitor", SAMPLE_DISPLAY_NAME))
        self.assertTrue(
            windows_monitor_matches("Generic PnP Monitor", SAMPLE_DISPLAY_NAME)
        )
        # An identifiable, different monitor is still skipped.
        self.assertFalse(windows_monitor_matches("DELL U2720Q", SAMPLE_DISPLAY_NAME))
        # No saved name means no filtering.
        self.assertTrue(monitor_matches("anything", ""))

    def test_single_generic_monitor_is_switched(self):
        switched, writes = switch_windows(["Generic PnP Monitor"], SAMPLE_DISPLAY_NAME)
        self.assertEqual(switched, 1)
        self.assertEqual(writes, [("handle0", 0x60, 0x12)])

    def test_two_generic_monitors_are_left_alone(self):
        # Two panels, neither carrying the saved name: writing to either could
        # black out the wrong screen, so nothing may be touched.
        switched, writes = switch_windows(
            ["Generic PnP Monitor", "Generic PnP Monitor"], SAMPLE_DISPLAY_NAME
        )
        self.assertEqual(switched, 0)
        self.assertEqual(writes, [])

    def test_named_match_selects_only_that_monitor(self):
        switched, writes = switch_windows(
            ["DELL U2720Q", SAMPLE_DISPLAY_NAME], SAMPLE_DISPLAY_NAME
        )
        self.assertEqual(switched, 1)
        self.assertEqual([handle for handle, _code, _value in writes], ["handle1"])

    def test_failed_write_is_not_counted(self):
        switched, writes = switch_windows(["Generic PnP Monitor"], "", ok=False)
        self.assertEqual(switched, 0)
        self.assertEqual(len(writes), 1)  # attempted once, not counted as switched


# --------------------------------------------------------------------------
# Version ordering
# --------------------------------------------------------------------------


class VersionOrderingTests(unittest.TestCase):
    """Self-update must compare versions numerically, not as strings."""

    CASES = [
        ("0.1.10", "0.1.9", True),   # numeric: "10" is newer than "9"
        ("0.1.9", "0.1.10", False),
        ("0.1.7", "0.1.6", True),
        ("0.1.6", "0.1.6", False),
        ("0.1.5", "0.1.6", False),
        ("1.0", "0.9.9", True),
        ("v0.2.0", "0.1.9", True),   # a tag name is accepted
        ("0.1.6.1", "0.1.6", True),
    ]

    def test_numeric_not_lexicographic(self):
        for candidate, current, expected in self.CASES:
            with self.subTest(candidate=candidate, current=current):
                self.assertEqual(version_is_newer(candidate, current), expected)

    def test_shorter_version_is_padded(self):
        # 0.2 is newer than 0.1.9, and neither 0.2 nor 0.2.0 is newer than the other.
        self.assertTrue(version_is_newer("0.2", "0.1.9"))
        self.assertFalse(version_is_newer("0.2.0", "0.2"))
        self.assertFalse(version_is_newer("0.2", "0.2.0"))

    def test_unparsable_candidate_is_never_newer(self):
        for candidate in ("junk", "", "latest"):
            with self.subTest(candidate=candidate):
                self.assertFalse(version_is_newer(candidate, "0.1.6"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def run_main(argv):
    """Call main(argv) and return (stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        main(argv)
    return out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    """The command names and the wiring from argv and KVM_CONFIG."""

    def setUp(self):
        # Only a config file on disk is created; nothing is installed or run.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_sample_config(self):
        path = self.tmp / "config.json"
        payload = {
            "devices": [{"id": vidpid, "name": name} for vidpid, name in SAMPLE_DEVICES],
            "display": SAVED_DISPLAY,
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def test_version_command(self):
        stdout, _stderr = run_main(["kvm-switcher.py", "version"])
        self.assertRegex(stdout.strip(), r"^\d+\.\d+\.\d+$")

    def test_version_flag_aliases(self):
        expected = run_main(["kvm-switcher.py", "version"])[0].strip()
        for flag in ("--version", "-V"):
            with self.subTest(flag=flag):
                self.assertEqual(run_main(["kvm-switcher.py", flag])[0].strip(), expected)

    def test_unknown_command(self):
        with self.assertRaises(SystemExit) as caught:
            run_main(["kvm-switcher.py", "nope"])
        message = str(caught.exception)
        self.assertTrue(message.startswith("Unknown command 'nope'"), message)

    def test_help_prints_the_docstring(self):
        stdout, _stderr = run_main(["kvm-switcher.py", "--help"])
        self.assertTrue(
            stdout.startswith("Watch USB devices and switch the monitor input"),
            stdout,
        )
        for command in ("configure", "status", "install", "uninstall", "switch", "update"):
            self.assertIn(command, stdout)

    def test_status_reads_the_config_named_by_env(self):
        # status() must pick up KVM_CONFIG, so a temp config is enough.
        path = self.write_sample_config()
        with patch.dict(os.environ, {"KVM_CONFIG": str(path)}):
            stdout, _stderr = run_main(["kvm-switcher.py", "status"])
        lines = stdout.splitlines()
        self.assertEqual(lines[: len(SAMPLE_DEVICES) + 1], device_lines(SAMPLE_DEVICES))
        self.assertEqual(
            lines[len(SAMPLE_DEVICES) + 1], f"Display: 0x12 HDMI-2 on {SAMPLE_DISPLAY_NAME}"
        )
        # Whether the login service exists is a property of the machine, so the
        # last line is pinned to one of the three known states, not to one state.
        self.assertIn(
            lines[-1],
            {"Watcher: running", "Watcher: installed, not running", "Watcher: not installed"},
        )

    def test_switch_refuses_without_a_configured_input(self):
        # Must exit before dispatching any switch command, after reading config.
        path = self.tmp / "config.json"
        path.write_text('{"devices": []}\n', encoding="utf-8")
        with patch.dict(os.environ, {"KVM_CONFIG": str(path)}), \
                patch("subprocess.run") as run:
            with self.assertRaises(SystemExit) as caught:
                run_main(["kvm-switcher.py", "switch"])
            run.assert_not_called()
        self.assertIn("No monitor input configured", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
