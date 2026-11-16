"""Remove what this tool installed, for `uninstall --purge`.

Order matters. The login entry goes first so nothing restarts the watcher, then
the settings, then the launcher, and the program directory last: on Windows a
running ``.pyz`` can delete its own archive and its own directory, but only
until something needs importing, so the fewer steps after that the better.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

from kvm_switcher import install_root, paths


def _remove_file(path: Path, removed: List[str], failed: List[str]) -> None:
    """Delete one file, recording whether it existed and whether it worked."""
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(str(path))
    except OSError:
        failed.append(str(path))


def _remove_dir(path: Path, removed: List[str], failed: List[str]) -> None:
    """Delete a directory tree. A missing directory is not a failure."""
    if not path.is_dir():
        return
    try:
        shutil.rmtree(path)
        removed.append(str(path))
    except OSError:
        failed.append(str(path))


def settings_files() -> List[Path]:
    """Every file holding saved choices: the config and any legacy sibling."""
    config = paths.config_path()
    found = [config, paths.watcher_pid_path()]
    legacy = paths.legacy_config_path(config)
    if legacy not in found:
        found.append(legacy)
    return found


def remove_settings() -> tuple:
    """Delete the config, the pid record, and their directory when it is empty.

    The directory is only removed when nothing else lives in it, so a
    ``KVM_CONFIG`` pointing into a shared folder cannot delete unrelated files.
    """
    removed: List[str] = []
    failed: List[str] = []
    for path in settings_files():
        _remove_file(path, removed, failed)
    directory = paths.config_path().parent
    try:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
            removed.append(str(directory))
    except OSError:
        failed.append(str(directory))
    return removed, failed


def _launcher_targets_this_install(launcher: Path, directory: Path) -> bool:
    """True when the launcher on PATH runs the program in `directory`.

    A copy run from somewhere else (a test, a temporary extraction) must not
    delete the launcher of the real installation, so the launcher is only
    removed when it actually names this program.
    """
    try:
        text = launcher.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if launcher.suffix.lower() == ".cmd":
        # The shim holds an absolute %LOCALAPPDATA% path expanded at write time.
        return str(directory).casefold() in text.casefold()
    # A POSIX launcher either symlinks the archive or execs it by absolute path.
    try:
        resolved = launcher.resolve()
        program = directory / "kvm-switcher.pyz"
        if program.exists() and resolved == program.resolve():
            return True
    except OSError:
        pass
    return str(directory) in text


def remove_program() -> tuple:
    """Delete the launcher and the install directory holding the program.

    Refuses inside a source checkout: there the "install" is the repository, and
    deleting it would destroy the working tree. A checkout is updated with git.

    Everything is resolved from the running program, and the launcher is only
    removed when it points here, so running a copy from a temporary directory
    cannot uninstall the real installation.
    """
    removed: List[str] = []
    failed: List[str] = []
    if install_root.running_from_checkout():
        return removed, failed
    directory = install_root.install_dir()
    launcher = paths.launcher_path()
    if _launcher_targets_this_install(launcher, directory):
        _remove_file(launcher, removed, failed)
    _remove_dir(directory, removed, failed)
    return removed, failed
