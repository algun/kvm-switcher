# kvm-switcher

Keeps one shared monitor showing the right computer: when the keyboard and mouse
arrive on a machine, this program selects that machine's input on the monitor.
It runs on Linux, macOS, and Windows.

## Why

This needs a hardware KVM that switches USB only. That switch moves the keyboard,
mouse, and other USB devices to the computer you are using, and it does not
change the monitor input. One monitor stays shared, so the picture stays on the
previous computer until you change the input by hand. This program watches for
the USB devices on the machine they were switched to and sets the monitor input
when one of them appears, so the display follows the peripherals.

Two requirements come with that design, and neither is optional:

- The KVM must switch **USB only**, and every machine must keep its own video
  cable to the monitor. If the KVM switches video, the monitor follows it by
  itself and there is nothing for this program to do.
- The program needs a working DDC/CI path to the monitor. It sends the standard
  input-select command; it cannot invent an input the monitor does not have, and
  it cannot undo a wrong switch (see [what it cannot do](#what-it-cannot-do)).

## Requirements

**Python 3.9 or newer.** Both installers check this by running the interpreter
before they write anything, and stop with the right command for your system if
it is missing or too old.

```text
winget install --id Python.Python.3.12 -e     # Windows
brew install python@3.12                      # macOS
sudo apt install python3                      # Debian/Ubuntu
```

| System | Also required |
| --- | --- |
| Linux | `ddcutil` (talks to the monitor) and `lsusb` from `usbutils` (lists USB devices). The login watcher uses systemd |
| macOS | [BetterDisplay](https://github.com/waydabber/BetterDisplay) with its [`betterdisplaycli`](https://github.com/waydabber/betterdisplaycli), installed with `brew install waydabber/betterdisplay/betterdisplaycli`. USB listing uses `ioreg`, which is already there |
| Windows | Nothing else. The monitor is driven through the Windows API and USB devices are read through `setupapi` |

There is nothing to `pip install`: the program uses only the standard library.

## Install

### The one-liners

On Linux or macOS:

```bash
curl -fsSL https://github.com/algun/kvm-switcher/releases/latest/download/install.sh | bash
```

On Windows:

```powershell
irm https://github.com/algun/kvm-switcher/releases/latest/download/install.ps1 | iex
```

`releases/latest/download` always resolves to the newest published release, so
the installer you run and the program it installs both come from the same tagged,
tested build. Nothing is fetched from a development branch, and an unreleased
commit cannot reach you.

If you would rather read the script before running it, which is always
reasonable for something piped into a shell, download it first:

```bash
curl -fsSLO https://github.com/algun/kvm-switcher/releases/latest/download/install.sh
bash install.sh
```

### macOS before you install

BetterDisplay must be installed and running, with CLI access on (that is the
default), because this program shells out to `betterdisplaycli`:

```bash
brew install waydabber/betterdisplay/betterdisplaycli
```

It also sends one direct DDC input-select command, and that is a licensing
question rather than a technical one: [BetterDisplay lists DDC input switching
as free](https://github.com/waydabber/BetterDisplay/wiki/List-of-free-and-Pro-features)
for personal use once the 14-day Pro trial ends, and other CLI features are Pro.
A business using BetterDisplay needs a Pro licence even for the features that are
free for personal use. Settle that before you standardise on this platform.

### From a checkout

```bash
./install.sh        # or: .\install.ps1
```

It installs the built `dist/kvm-switcher.pyz` when you have one (build it with
`python build.py`), and otherwise downloads the latest release.

### What lands on disk

| System | Program | Command on `PATH` |
| --- | --- | --- |
| Linux, macOS | `~/.local/share/kvm-switcher/kvm-switcher.pyz` | `~/.local/bin/kvm-switcher`, a small launcher written by the installer |
| Windows | `%LOCALAPPDATA%\kvm-switcher\kvm-switcher.pyz` | `%USERPROFILE%\bin\kvm-switcher.cmd`, with `%USERPROFILE%\bin` added to your user `PATH` |

The installer does not start anything. Open a new terminal afterwards, because
a shell that was already running keeps its old `PATH`.

## First run

```bash
kvm-switcher configure    # pick the USB devices and the monitor input
kvm-switcher status       # show the saved choices and whether the watcher runs
kvm-switcher install      # start the watcher now and at every login
```

`configure` lists the USB devices present, and the input codes your monitor
advertises, with names such as `HDMI-1` or `DisplayPort-1`. On macOS it cannot
read that list, so it asks for the input code and the BetterDisplay tag by hand.
Your choices are saved to `~/.config/kvm-switcher/config.json`
(`%APPDATA%\kvm-switcher\config.json` on Windows); set `KVM_CONFIG` to use a
different file. See [docs/design.md](docs/design.md) for the file format.

Once installed, the watcher switches the monitor when a selected device appears,
and re-arms when it goes away, so moving the KVM back and forth keeps working.

## Commands

```text
kvm-switcher                 watch in this window; offers to install at login
kvm-switcher configure       pick devices, the monitor, and its input
kvm-switcher status          show saved choices and whether the watcher is running
kvm-switcher install         run at login
kvm-switcher uninstall       stop it, then ask what else to remove
kvm-switcher switch [code]   switch once, or use the saved input
kvm-switcher check           report whether a newer version is published
kvm-switcher update          replace this program with the published version
kvm-switcher version         print the version
```

`update` downloads the published program, verifies it, and says plainly whether
the new version is live now or takes effect on the next launch.

`uninstall` always removes the login entry and stops the watcher, then asks two
questions: whether to remove your saved settings (devices and monitor input),
and whether to remove the program itself. Answering no keeps them, so you can
reinstall without picking your devices again. For scripted use, `--keep-settings`
removes only the login entry and `--purge` removes everything without asking.
[docs/design.md](docs/design.md) explains the rest.

## When something goes wrong

`'python' is not recognized`, `kvm-switcher` not found right after installing,
`ERROR: Access is denied.` during `install`, a screen that flashes twice per
switch, or a monitor that goes black after `switch`: each one is covered in
[docs/troubleshooting.md](docs/troubleshooting.md) with the cause and the fix.

## What it cannot do

- **It cannot recover a monitor that is showing nothing.** If the saved input
  code is wrong, or that input has no active source, the screen goes black and
  the program cannot tell. Bring the picture back with the monitor's own input
  button, then run `configure` again.
- **It cannot work with a KVM that switches video.** It needs a USB-only KVM,
  with each machine keeping its own video cable to the shared monitor.
- **It cannot help on a machine where DDC/CI is blocked or unsupported.** The
  program sends a standard command and reports what the OS returned; a monitor
  or cable that drops DDC traffic gets no switch.

Building, testing, and releasing are in [CONTRIBUTING.md](CONTRIBUTING.md).
