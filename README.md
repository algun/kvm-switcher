# kvm-switcher

## Why

This needs a hardware KVM that switches USB only. That switch moves the keyboard, mouse, and other USB devices to the computer you are using, and it does not change the monitor input. One monitor stays shared, so the picture stays on the previous computer until you change the input by hand. This program watches for those USB devices on the machine they were switched to and sets the monitor input when one of them appears, so the display follows the peripherals. Without that USB KVM there is nothing for it to watch.

It runs on Linux (`ddcutil`), macOS (BetterDisplay), and Windows (DDC/CI).

Settings live in `~/.config/kvm-switcher/config.json` (`%APPDATA%\kvm-switcher\config.json` on Windows). Override the path with `KVM_CONFIG`. `config.json` in this repo is only the USB-device seed copied on first run. An older `devices.txt` next to the JSON file is still read until the next `configure`, which rewrites `config.json`.

## Dependencies

Python 3 is the only language runtime. The program uses the standard library, so there is nothing to `pip install`. Python is not built into every system: many Linux installs already have `python3`, but macOS and Windows do not. Install it from python.org or your package manager. The Unix scripts call `python3`; the PowerShell scripts call `python`.

| System | Also required |
| --- | --- |
| Linux | `ddcutil` (switch the monitor), `lsusb` from `usbutils` (list USB devices), systemd (login service) |
| macOS | BetterDisplay's `betterdisplaycli` (switch the monitor). USB listing uses `ioreg`, which is already installed |
| Windows | Nothing else. Display switching uses the Windows API, and USB listing uses PowerShell |

## Install

From a checkout:

```bash
./install.sh
```

```powershell
.\install.ps1
```

Without a checkout, the same scripts download the latest GitHub release. On Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/algun/kvm-switcher/v0.1.0/install.sh | bash
```

On Windows:

```powershell
irm https://raw.githubusercontent.com/algun/kvm-switcher/v0.1.0/install.ps1 | iex
```

That puts `kvm-switcher` on `PATH` (`~/.local/bin` on Linux and macOS, `%USERPROFILE%\bin\kvm-switcher.cmd` on Windows). It does not start the login watcher. Run `kvm-switcher configure`, then `kvm-switcher install` if you want it at login.

To publish a release, bump `VERSION` in `kvm-switcher.py`, commit, and push a matching tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The tag must be `v` plus `VERSION`. The release workflow runs the tests and attaches `kvm-switcher.tar.gz` and `kvm-switcher.zip`.

## Quick start

```bash
python3 kvm-switcher.py configure   # pick USB devices, the monitor, and its input
python3 kvm-switcher.py status      # saved choices and whether the watcher is running
python3 kvm-switcher.py             # watch in this window; asks to install at login
```

On Windows, run `python kvm-switcher.py` with the same commands.

`install` copies the program to `~/bin` and starts it at login (a systemd user service, a launchd agent, or a scheduled task). `uninstall` removes that entry and leaves `config.json` in place.

## Commands

```text
kvm-switcher.py                 watch (asks to install on first interactive run)
kvm-switcher.py configure       pick devices, the monitor, and its input
kvm-switcher.py status          show saved choices and whether the watcher is running
kvm-switcher.py install         run at login
kvm-switcher.py uninstall       stop it and remove the login entry
kvm-switcher.py switch [code]   switch once, or use the saved input
kvm-switcher.py version         print the version
```

`install.sh` and `install.ps1` put `kvm-switcher` on `PATH`. `configure` asks `ddcutil` which monitors are connected and which input codes each one advertises, then saves the monitor (`bus`, `name`) and the chosen input. On macOS and Windows that list is not available from here, so configure asks for the input code and, on macOS, the BetterDisplay tag. Nothing is chosen until then. `switch` with no code uses that saved input.

```json
{
  "devices": [{"id": "1234:abcd", "name": "USB receiver"}],
  "display": {"name": "Monitor", "bus": "5", "input": "0x0f", "inputLabel": "DisplayPort-1"}
}
```

## Tests

Automated tests cover JSON and legacy config, display detection text, status text, the device catalog, switch command lines, and shell syntax. They do not talk to a monitor or USB bus.

```bash
python3 -m unittest discover -s tests -v
```

No extra packages. The stdlib `unittest` runner is enough.

Check by hand when you change login install or the actual display switch:

- `kvm-switcher.py switch` moves the monitor to the saved input
- plugging a selected device switches once, and unplugging arms it again
- `install` / `uninstall` register and remove the login entry (systemd user service, launchd agent, or Scheduled Task)
