# Contributing

The program is a Python package under `src/kvm_switcher/`, shipped as one
zipapp. This file covers what you need to change it: the layout, the test
command, building the artifact, and cutting a release.

## Setup

Python 3.9 or newer, and git. The test suite is the only thing to install, and
pytest is the only dependency:

```bash
git clone https://github.com/algun/kvm-switcher
cd kvm-switcher
python -m pip install pytest
```

`pip install -e ".[dev]"` does the same in one step if you would rather have the
package importable outside the source tree, but it is not required: pytest is
configured with `pythonpath = ["src"]`, so the suite runs against the working
copy.

To run the program from the source tree without installing it, put `src` on the
import path:

```bash
PYTHONPATH=src python -m kvm_switcher --help     # POSIX
$env:PYTHONPATH="src"; python -m kvm_switcher --help   # PowerShell
```

Keep `git` on `PATH`. One test shells out to `git ls-files` to scan the files git
tracks for mojibake and BOMs, and it falls back to walking the working tree when
git is missing, so a local run without git is quieter but weaker.

## Layout

```text
src/kvm_switcher/
  __init__.py        __version__, and nothing else
  __main__.py        promotes a staged update, then runs the CLI
  cli.py             argv dispatch and the interactive prompts
  watcher.py         the polling loop and the single-instance guard
  update.py          check, download, verify, stage
  promote.py         applies a staged .pyz at the next launch
  install_root.py    "where am I installed" - the only __file__ reasoning
  paths.py config.py mccs.py
  devices/           __init__.py dispatches; linux.py darwin.py windows.py
  displays/          __init__.py dispatches; linux.py darwin.py windows.py
  process/           __init__.py; posix.py windows.py
  service/           __init__.py; systemd.py launchd.py windows.py
tests/               conftest.py, test_kvm_switcher.py, test_characterisation.py,
                     test_delivery.py
build.py             builds dist/kvm-switcher.pyz
```

Three conventions hold the cross-platform story together:

- Anything Windows-only is imported or bound **inside a function**, never at
  module scope, so importing the package on Linux or macOS cannot fail. A test
  enforces this for the `ctypes` entry points.
- The per-OS modules are reached through the package `__init__.py`, which is the
  single dispatch point. Tests target `displays.windows` and friends directly
  rather than patching a platform key where the call happens.
- The installed program is one file, so nothing may assume the package sits in a
  real directory. "Where am I installed?" is a question for `install_root.py`,
  and `docs/design.md` explains why the program is a zipapp at all.

## Tests

```bash
python -m pytest -q
```

- `tests/test_characterisation.py` locks observable behaviour: the exact
  `status` text, the MCCS input mapping, the device-name rules and the
  one-monitor switch rule. If an assertion there has to change, the change is
  visible to users, so say so in the commit. Its fixtures are synthetic, not
  anyone's hardware.
- `tests/test_delivery.py` covers the zipapp: payload verification, staging,
  promotion, and a test that pins *why* a running archive is never swapped in
  place.
- Nothing in the suite talks to a monitor, a USB bus or the login service. The
  one test that reads the real USB bus is opt-in with
  `KVM_CHARACTERISATION_LIVE=1`.

CI runs the suite on Ubuntu, Windows and macOS at Python 3.9 and 3.12, and then
builds the zipapp and runs it on each, because the platform that breaks the
program is not the one you develop on.

Behaviour that no test can cover - the actual display switch and the login
registration - is worth a manual check when you touch it: switch a real monitor,
confirm a selected device triggers once and re-arms when unplugged, and confirm
`install` / `uninstall` add and remove the login entry. Two machines differ in
both devices and inputs, so compare the run against what this hardware reported
before your change rather than against anyone else's list.

## Building the zipapp

```bash
python build.py
```

That writes `dist/kvm-switcher.pyz`, the file users download. It needs nothing
beyond the standard library, and it is reproducible: entries are written in
sorted order with a fixed timestamp and without `__pycache__`, so the same
sources build byte-identical output.

```bash
python build.py --output dist/other.pyz          # somewhere else
python build.py --expect-version 0.2.2           # fail unless it reports this
```

`--expect-version` is the release check: it imports `__version__` out of the
built archive, so it verifies the artifact a user runs rather than a regex over
the source. Run the result directly to be sure:

```bash
python dist/kvm-switcher.pyz version
python dist/kvm-switcher.pyz --help
```

## Releasing

The version is declared once, in `src/kvm_switcher/__init__.py`. There is no
second copy: `build.py`, `release.yml` and `update.py` all read it there.

1. Bump `__version__` in `src/kvm_switcher/__init__.py`.
2. Run `python -m pytest -q`.
3. Run `python build.py --expect-version <version>`.
4. Commit, push `main`, and push a new tag named `v` plus the version:

```bash
git tag v0.2.5
git push origin v0.2.5
```

The tag must equal `__version__` exactly, minus the leading `v`, and it must be
a *new* tag: pushing a commit does not move an existing tag. `release.yml` fails
the release if the built archive reports anything other than the tag, which is
what makes a mismatch impossible to publish rather than merely unlikely.

On a matching tag the workflow runs the tests, builds the zipapp with the version
check, confirms the archive runs, and publishes these assets:

- `kvm-switcher.pyz` - the program. This name is fixed, because `update` and both
  installers fetch `releases/latest/download/kvm-switcher.pyz`.
- `kvm-switcher.tar.gz` and `kvm-switcher.zip` - archives of the program,
  `config.json`, the installers and `build.py`, for anyone who wants them.

The tag is what triggers all of it, so nothing is published by pushing `main`: a
commit that lands without a tag changes what the installer one-liners fetch, but
never reaches anyone through `update`.

Two things to be deliberate about in release notes:

- An install of the pre-package single file cannot take a zipapp update. `update`
  refuses it and tells the user to re-run the installer once.
- `update` stages the new archive and applies it at the next launch, so the
  message a user sees distinguishes "live now" from "takes effect on next
  launch". Do not describe an update as immediate.

## Text and encoding

Files are UTF-8 without a BOM, and ASCII apart from one character: the em dash
(U+2014) inside the watcher's "USB device detected" message, which is what users
read in the log. It is there because it was already there, not as a style
choice, so do not add others. A test fails on a BOM or on cp1252-style mojibake,
because both have happened here before.
