"""Apply a downloaded update the next time the program starts.

``update()`` cannot safely replace the archive it is running from, so it writes
``kvm-switcher.pyz.new`` beside it and this module finishes the job.

Why the swap is deferred rather than done in place. A probe on Windows (see the
note in ``kvm_switcher.update``) showed ``os.replace`` over a running ``.pyz``
does *not* raise ``WinError 32``: the rename succeeds. The damage is subtler and
worse. zipimport caches the archive's table of contents, so after the swap any
module imported for the first time is read by its old offset and length. In the
probe that produced ``EOFError: EOF read where not expected``, a stale module
object with the wrong attributes, and, for a module that only exists in the new
payload, ``ModuleNotFoundError``.

So promotion has two hard rules:

* it happens before anything imports from the archive, and
* the promoting process never imports from the swapped archive: it re-launches a
  fresh interpreter and returns that process's exit code.

Promoting at the top of :mod:`kvm_switcher.__main__` satisfies both, because
``__main__`` is the only code that has to run before the package is imported.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Optional

from kvm_switcher import install_root

# The suffix ``update()`` writes and this module consumes.
PENDING_SUFFIX = ".new"

# A downloaded archive is only promoted when it really is one, so a truncated
# file can never replace a working install.
_REQUIRED_MEMBER = "kvm_switcher/__init__.py"


def pending_path(target: Path) -> Path:
    """The file an update is staged in, beside the program it will replace."""
    return target.with_name(target.name + PENDING_SUFFIX)


def is_complete(path: Path) -> bool:
    """True when the file is a zip archive that carries the package."""
    try:
        with zipfile.ZipFile(path) as archive:
            return _REQUIRED_MEMBER in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _relaunch(target: Path, argv: List[str]) -> int:
    """Run the newly installed archive in a fresh interpreter and return its code."""
    command = [sys.executable, str(target)] + list(argv[1:])
    try:
        return subprocess.run(command).returncode
    except OSError as exc:
        print("Updated, but could not restart {}: {}".format(target, exc), file=sys.stderr)
        return 1


def promote_pending(argv: Optional[List[str]] = None) -> Optional[int]:
    """Apply a staged update and re-launch, or return None to carry on as normal.

    Returning None means "nothing to do, this process may run": either there is
    no staged update, or it could not be applied. A failed promotion is not an
    error the user needs to act on, because the staged file stays put and the
    next launch tries again.
    """
    if argv is None:
        argv = sys.argv
    target = install_root.archive_path()
    if target is None:
        # A directory install has no single file to swap.
        return None
    staged = pending_path(target)
    if not staged.is_file():
        return None
    if not is_complete(staged):
        # Never leave a broken payload to be retried on every launch.
        try:
            staged.unlink()
        except OSError:
            pass
        print(
            "Discarded an incomplete update at {}; still running {}.".format(
                staged, target
            ),
            file=sys.stderr,
        )
        return None
    if not sys.executable:
        # Without an interpreter to restart we cannot finish the swap safely.
        return None
    try:
        os.replace(str(staged), str(target))
    except OSError as exc:
        # Typically another process holds the archive open on Windows. The
        # staged file survives, so the update lands on a later launch.
        print("Could not apply the update yet: {}".format(exc), file=sys.stderr)
        return None
    return _relaunch(target, list(argv))
