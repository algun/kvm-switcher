"""Answer "where is the running program installed".

The one place in the package that is allowed to reason about ``__file__``, so
that a plain directory install and a ``.pyz`` zipapp both work. Seeding the
config, install, self-update, the checkout guard, and spawning the watcher all
ask this module instead of guessing.

Inside a zipapp ``__file__`` is ``.../kvm-switcher.pyz/kvm_switcher/paths.py``,
a path that does not exist on disk. The archive is recovered by looking for a
zipapp suffix in a path component, which needs no filesystem access, and every
answer below is built from that archive rather than from a bogus directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

# Suffixes a Python zipapp can carry. `.pyz` is what the build produces; the
# windowed spelling is accepted too so detection survives a rename.
_ZIP_SUFFIXES = (".pyz", ".pyzw")

# The single file a pre-package install ran. It is still a valid install until
# the zipapp cutover, and update() replaces it in place.
LEGACY_PROGRAM_NAME = "kvm-switcher.py"

# `src/kvm_switcher` in a checkout, `<install dir>/kvm_switcher` once installed,
# and `<archive>/kvm_switcher` inside a zipapp.
_PACKAGE_DIR = Path(__file__).resolve().parent


def _absolute(path: object) -> Path:
    """Absolute but unresolved: the file need not exist, so never stat it."""
    return Path(os.path.abspath(str(path)))


def _zip_archive(text: str) -> Optional[Path]:
    """The archive a path points into, or None when it is an ordinary path.

    Only component names are inspected, never the filesystem, because inside a
    zipapp the path does not exist.
    """
    if not text:
        return None
    parts = Path(text).parts
    for index, part in enumerate(parts):
        if part.lower().endswith(_ZIP_SUFFIXES):
            return Path(*parts[: index + 1])
    return None


def _candidates() -> List[str]:
    """Paths that could name the running program, most trustworthy first.

    ``sys.argv[0]`` is what the interpreter was actually told to run, so it
    survives a zipapp intact; ``__file__`` is the fallback for a directory
    install, where it is a real file.
    """
    found: List[str] = []
    argv = getattr(sys, "argv", None) or []
    if argv and argv[0]:
        found.append(str(argv[0]))
    found.append(str(__file__))
    return found


def archive_path() -> Optional[Path]:
    """The ``.pyz`` this code runs from, or None for a directory install."""
    for candidate in _candidates():
        archive = _zip_archive(candidate)
        if archive is not None:
            return _absolute(archive)
    return None


def is_zipapp() -> bool:
    """True when the program is a zipapp rather than a directory of modules."""
    return archive_path() is not None


def import_dir() -> Path:
    """Directory to put on ``sys.path`` so ``kvm_switcher`` resolves.

    For a zipapp that is the archive itself, which the interpreter imports from
    directly; for a directory install it is the directory holding the package.
    """
    archive = archive_path()
    if archive is not None:
        return archive
    return _absolute(_PACKAGE_DIR.parent)


def checkout_root() -> Optional[Path]:
    """Repository root when this is a source checkout, otherwise None.

    Both layouts are checked: ``src/kvm_switcher`` puts the repository one level
    above the import directory, a flat checkout puts it at the same level.
    """
    directory = import_dir()
    for candidate in (directory, directory.parent):
        if (candidate / ".git").is_dir():
            return candidate
    return None


def running_from_checkout() -> bool:
    """True when the program sits in a source checkout rather than an install."""
    return checkout_root() is not None


def install_dir() -> Path:
    """Directory the program is installed into, where its seed config lives."""
    archive = archive_path()
    if archive is not None:
        return archive.parent
    return _absolute(_PACKAGE_DIR.parent)


def legacy_program_path() -> Optional[Path]:
    """The single-file program a pre-package install left beside the package."""
    for directory in (install_dir(), checkout_root()):
        if directory is None:
            continue
        candidate = directory / LEGACY_PROGRAM_NAME
        if candidate.is_file():
            return candidate
    return None


def program_path() -> Optional[Path]:
    """The one file self-update replaces, or None when there is no single file.

    A zipapp is one file, and so is a pre-package ``kvm-switcher.py`` install.
    A plain directory install has neither, so update() reports that instead of
    inventing a target and recreating the duplicate install this refactor removed.
    """
    archive = archive_path()
    if archive is not None:
        return archive
    return legacy_program_path()


def seed_path() -> Optional[Path]:
    """The ``config.json`` shipped beside the program, or None when there is none."""
    for directory in (install_dir(), checkout_root()):
        if directory is None:
            continue
        candidate = directory / "config.json"
        if candidate.is_file():
            return candidate
    return None


def _bootstrap_argv() -> List[str]:
    """A ``-c`` launcher that imports the package without any environment setup.

    Used only for a directory install outside ``sys.path``. Being a single argv
    list, it works unchanged for ``Popen``, systemd, launchd and the Windows
    Task Scheduler, so no service has to know about ``PYTHONPATH``. Arguments
    after the launcher are appended to ``sys.argv``, so a subcommand reaches
    ``main`` exactly as it would through ``python -m kvm_switcher``.
    """
    code = (
        "import sys; sys.path.insert(0, {path!r}); "
        "from kvm_switcher.cli import main; "
        "raise SystemExit(main(['kvm-switcher'] + sys.argv[1:]))"
    ).format(path=str(import_dir()))
    return [sys.executable, "-c", code]


def program_argv() -> List[str]:
    """Command that runs this installation, without the subcommand."""
    target = program_path()
    if target is not None:
        return [sys.executable, str(target)]
    return _bootstrap_argv()
