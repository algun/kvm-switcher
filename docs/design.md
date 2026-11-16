# Design notes

Why it works the way it does, and the details that are easy to get wrong if you
change it. For installing and everyday use, read the [README](../README.md).

## How a switch is detected

The program polls the USB bus every half second and asks one question: is any of
the saved devices present? A device that was absent and is now present means the
KVM has been moved to this machine, so the monitor input is set and the check
re-arms. Nothing switches again while the device stays plugged in, and a failed
switch is not retried on every poll, because that would flash the screen until it
succeeded.

Only the arrival matters. The program never tries to reverse a switch, and it
does not know whether the monitor actually changed.

## Per-OS behaviour

### Linux

- USB devices come from `lsusb`, with the kernel's root hubs (`1d6b:*`)
  skipped: they are always present and would make the trigger permanently on.
- Monitors come from `ddcutil detect`, with each display's advertised input list
  read from `ddcutil --bus N capabilities` (`Feature: 60`). A display `ddcutil`
  calls invalid is dropped.
- The switch is `ddcutil --bus N setvcp 60 <code>`, and the program exits with
  `ddcutil`'s own exit code, so a failure is visible in `$?`.
- The login watcher is a systemd user unit at
  `~/.config/systemd/user/kvm-switcher.service`, with `Restart=always` so a
  crash does not silently end the watching.

### macOS

- USB devices come from `ioreg -p IOUSB -l -w0 -x`, pairing each `idVendor`
  with its `idProduct`.
- There is no discovery of monitor inputs: BetterDisplay owns the DDC channel,
  so `configure` asks for the input code and the BetterDisplay tag id by hand.
- The switch is `betterdisplaycli set --tagID=<tag> --feature=ddc
  --vcp=inputSelect --value=<code>`. The binary is taken from `PATH`, falling
  back to `/opt/homebrew/bin/betterdisplaycli`.
- The login watcher is a LaunchAgent at
  `~/Library/LaunchAgents/com.kvm-switcher.watcher.plist`, with `KeepAlive`.
- BetterDisplay licensing is the one thing to settle before standardising on
  this platform, and it is covered at the install step in the README.

### Windows

- USB devices are read in-process through `setupapi`, which costs microseconds
  per poll and keeps the product string a device reports about itself
  (`DEVPKEY_Device_BusReportedDeviceDesc`). When `setupapi` is unusable the
  program falls back to a PowerShell `Get-PnpDevice` query. Class names such as
  `USB Composite Device` are not treated as names.
- Monitors are enumerated through DXVA2 (`EnumDisplayMonitors` plus
  `GetPhysicalMonitorsFromHMONITOR`). Each monitor is asked for its MCCS
  capability string with `GetCapabilitiesStringLength` and
  `CapabilitiesRequestAndCapabilitiesReply`, and the `60(...)` values in it
  become the input list, labelled from the shared MCCS table. The reply buffer
  is a byte buffer on purpose: the length is in ANSI bytes, and a wide buffer
  interleaves the reply into mojibake.
- The switch is `SetVCPFeature(handle, 0x60, value)` on every monitor that
  matches, and the count is printed (`Switched 1 monitor(s) to 0x12`).
- DXVA2 reports most panels as `Generic PnP Monitor`, so the display name shown
  by `configure` comes from WMI (`WmiMonitorID.UserFriendlyName`).
- The login watcher is a Scheduled Task (`schtasks /SC ONLOGON /RL LIMITED`). A
  machine where `schtasks` needs elevation gets a per-user `Run` registry entry
  instead, and the program says so rather than failing.
- The running watcher records its pid in `%APPDATA%\kvm-switcher\watcher.pid`,
  which is how `status` and `uninstall` find it.

## The one-monitor generic-name rule

The saved monitor name is a case-insensitive substring match against what the OS
reports. That works everywhere except Windows, where the reported description is
often `Generic PnP Monitor` and can never contain the real model name.

So on Windows a description is also accepted when it is one of those
placeholders, but **only when exactly one monitor is attached**. With two or more
panels and a name that matches none of them, nothing is switched: writing the
input code to the wrong screen blacks out the screen someone is working on, which
is worse than not switching. A monitor with a distinct, non-generic name is never
silently replaced by a placeholder match.

## Why a zipapp

The program ships as one archive, `dist/kvm-switcher.pyz`, built by `build.py`
and run directly by the interpreter. The reasons it is one file rather than a
package directory or a wheel:

- Installation is one file to copy, and login registration names that one path,
  so there is no second copy to drift. The old arrangement had the installer
  place a program and the service register a *different* copy, and that duplicate
  is what produced two watchers and a double flash.
- `update` has one file to replace and one file to verify.
- Nothing is unpacked, so `uninstall` and `install` never have to reason about a
  directory tree that a user may have moved.

The cost is that `__file__` is useless inside an archive: it points at
`.../kvm-switcher.pyz/kvm_switcher/paths.py`, which does not exist. Every
question of the form "where am I installed?" therefore goes through
`install_root.py`, which recovers the archive path itself.

## Config file format

`configure` writes JSON to `~/.config/kvm-switcher/config.json`
(`%APPDATA%\kvm-switcher\config.json` on Windows). `KVM_CONFIG` overrides the
path, and `KVM_DEVICES_FILE` is still honoured as the older spelling of it.

```json
{
  "devices": [
    {"id": "1234:abcd", "name": "USB receiver"}
  ],
  "display": {
    "name": "Monitor",
    "bus": "5",
    "input": "0x0f",
    "inputLabel": "DisplayPort-1"
  }
}
```

- `devices` is the trigger list, as lowercase `vid:pid` with the name to show.
  Only the ids are matched; the names exist so `status` is readable.
- `display.input` is the MCCS input-select value, kept in the form the monitor
  advertises (`0x0f`). `display.bus` is the `ddcutil` bus on Linux and
  `display.tag` the BetterDisplay tag on macOS; `name` is what the switch
  matches against. Display keys whose value is empty are dropped when the file is
  written, so an unused field never appears.
- A malformed JSON file stops the program with the file path and the parser
  error, rather than silently falling back to defaults.
- A `devices.txt` from before the JSON format is still read when `config.json` is
  absent, and the next `configure` rewrites the JSON file. `config.json` in this
  repository is only the seed copied on first run when nothing is saved.

The watcher's pid and start time live in `watcher.pid` beside the config. The
start time is what stops a recycled pid from being treated as the watcher.

## Update semantics

`update` fetches `releases/latest/download/kvm-switcher.pyz`, verifies the
download, and then has to deal with a fact that is easy to miss: the archive it
is running from cannot be replaced underneath the running process. A probe
showed `os.replace` over a live `.pyz` does not even fail on Windows; it
succeeds, and then zipimport reads later modules at the previous archive's
offsets, producing `EOFError`, wrong module contents, or `ModuleNotFoundError`.

So the payload is **staged**, not swapped:

1. Downloaded bytes are checked before anything is touched: the file must open
   as a zip, contain `kvm_switcher/__init__.py`, compile every module it
   contains, declare `__version__`, and be newer than the running version.
2. The verified archive is written as `kvm-switcher.pyz.new` beside the program.
3. At the next launch, the program looks for a staged file before it runs
   anything else. If the staged file is a complete archive it replaces the
   program and a fresh interpreter relaunches the same command, so no module is
   ever imported from an archive that changed underneath it; if the staged file
   is damaged it is discarded and the existing program runs; if the swap is
   blocked, the staged file stays for the next attempt.

What that means for the person running it: `update` says either
`Restarted the watcher, so <version> is live now.` when it owned the running
watcher and restarted it, or `The new version takes effect on the next launch,
not now.` when nothing would apply the staged file on its own. Neither message is
a rounding of the other.

Two related rules:

- `update` refuses to run inside a git checkout; use `git pull` there.
- An install of the pre-package single-file program (a standalone `.py`) is
  refused with instructions to re-run the installer once, because writing a
  zipapp over that name would leave a file whose name lies about its contents,
  and both the Windows shim and the login entry name that path explicitly.
