# Troubleshooting

Five failures that have actually happened, with what causes them and what to do.
Everything here is a message or a behaviour the shipped program produces, not a
guess.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `'python' is not recognized as an internal or external command`, or typing `python` opens the Microsoft Store | No Python installed; or the Windows Store *alias stub* is on `PATH`, which is a `python.exe` that exists and only opens the Store, so an existence check passes while nothing runs | Install Python 3.9 or newer, then open a new terminal: `winget install --id Python.Python.3.12 -e`. If `python` still opens the Store, turn the stub off under **Settings > Apps > Advanced app settings > App execution aliases** and clear `python.exe` and `python3.exe` |
| `kvm-switcher` is not found right after the installer said `Installed ...` | The installer added a directory to `PATH`, but a shell that was already open keeps the `PATH` it started with | Open a new terminal. On Windows the installer says `Open a new terminal for it to take effect.`; on Linux or macOS, if the launcher directory was not already on `PATH`, it prints an `Add ... to PATH to run kvm-switcher.` line naming that directory - add it to your shell profile, then open a new terminal |
| `ERROR: Access is denied.` while running `kvm-switcher install` | `schtasks` needs elevation on some machines, so creating the Scheduled Task is refused | Expected, and not fatal: the program says `Scheduled Task could not be created (schtasks exit ...); using the per-user Run key instead.` and writes the login entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` instead. Run the command from an elevated terminal if you would rather have the task |
| The screen flashes twice for one switch | Two watchers were running, each switching on the same detection. Two copies came from a pre-package install that registered a duplicate program beside the installed one | Fixed: the watcher now refuses to start when one is already running for this install, and the installer no longer leaves a second copy. Clear an old one with `kvm-switcher uninstall`, then `kvm-switcher install` once |
| The monitor goes black after `kvm-switcher switch` | The saved input code is wrong, or that input has no active source attached to it | Recover first, with the monitor's own input button: the program cannot undo a black screen. Then run `kvm-switcher configure` again and pick the input that the machine is really plugged into. `kvm-switcher status` shows the saved code and label |

## If none of these match

- `kvm-switcher status` prints the saved devices, the saved monitor input, and
  whether the watcher is installed and running. Most reports are answered there.
- `kvm-switcher check` says whether a newer version is published, and
  `kvm-switcher update` installs it. An old watcher can be the whole problem, so
  update before investigating further.
- A watcher started by hand in a terminal (`kvm-switcher` with no subcommand)
  prints a `USB device detected` line on each switch, which shows whether
  detection or the switch itself is failing.
