"""Checking for and applying a published release.

Verifies the downloaded payload before touching anything installed, and refuses
to overwrite a source checkout, which must be updated with git instead.

Two payload shapes are understood, because both exist in the wild:

``kvm-switcher.pyz``
    The packaged program. It cannot be swapped under a running process safely
    (see below), so it is staged beside the program and applied by
    :mod:`kvm_switcher.promote` at the next launch.

``kvm-switcher.py``
    The pre-package single file. Read in full at startup, so replacing it in
    place is safe and takes effect immediately. Only an install that is itself
    that single file can still receive one, which is the legacy path.

Why a running zipapp is never swapped in place. A probe on Windows found that
``os.replace`` over the running ``.pyz`` does **not** raise ``WinError 32``: the
rename succeeds. The damage is subtler and worse. zipimport caches the archive's
table of contents, so a module first imported after the swap is read at its old
offset and length. The probe produced ``EOFError: EOF read where not expected``,
a module object with the wrong attributes, and ``ModuleNotFoundError`` for a
module present only in the new payload. So the new archive is staged as
``kvm-switcher.pyz.new`` and promoted by a process that has not imported the
package yet. ``update()`` says which of the two happened, because "updated" that
takes effect later is a different promise from "updated and running now".
"""

from __future__ import annotations

import io
import os
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from kvm_switcher import __version__, install_root, promote
from kvm_switcher.process import same_program, spawn_watcher

UPDATE_REPO = "algun/kvm-switcher"
# The release asset every install now runs, and what install.ps1/install.sh
# fetch. Keep the three in step.
UPDATE_ASSET = "kvm-switcher.pyz"
# `latest` resolves to the newest non-prerelease tag, so an installed copy
# follows releases rather than the tip of main.
UPDATE_URL = "https://github.com/{}/releases/latest/download/{}".format(UPDATE_REPO, UPDATE_ASSET)

# The single file a pre-package release published, still understood so an
# install that has not migrated can be verified (and told to re-install).
LEGACY_PROGRAM_NAME = install_root.LEGACY_PROGRAM_NAME
# The package member that proves a zip really is this program.
_ENTRY_MEMBER = "kvm_switcher/__init__.py"
# A zipapp carries the package version in its `__init__.py`.
_VERSION_RE = re.compile(rb'^__version__\s*=\s*"([^"]+)"', re.M)
# The old single-file layout carried `VERSION = "..."`.
_LEGACY_VERSION_RE = re.compile(rb'^VERSION = "([^"]+)"', re.M)


def version_tuple(text: str):
    """Comparable form of a dotted version, tolerating a leading v and suffixes."""
    parts = text.strip().lstrip("vV").split(".")
    numbers = []
    for part in parts:
        digits = re.match(r"\d+", part)
        if not digits:
            break
        numbers.append(int(digits.group(0)))
    return tuple(numbers)


def version_is_newer(candidate: str, current: str) -> bool:
    """Numeric comparison, padded so 0.2 beats 0.1.9 but not 0.1.9.1."""
    candidate_parts = version_tuple(candidate)
    current_parts = version_tuple(current)
    if not candidate_parts:
        return False
    width = max(len(candidate_parts), len(current_parts))
    pad = lambda parts: parts + (0,) * (width - len(parts))  # noqa: E731
    return pad(candidate_parts) > pad(current_parts)


def _url(ref: Optional[str] = None) -> str:
    """Where the published program is fetched from.

    No ref means the newest release asset, which is what an installed copy
    should follow. A ref names a tag or branch on the raw host, which serves the
    file straight out of the repository.
    """
    if not ref:
        return UPDATE_URL
    return "https://raw.githubusercontent.com/{}/{}/{}".format(UPDATE_REPO, ref, UPDATE_ASSET)


def _download(ref: Optional[str] = None, timeout: float = 30.0) -> bytes:
    url = _url(ref)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit("Could not download {}: {}".format(url, exc)) from exc


def is_zip_payload(body: bytes) -> bool:
    """True when the download is a zipapp rather than a legacy single file."""
    return zipfile.is_zipfile(io.BytesIO(body))


def _zip_version(body: bytes) -> Optional[str]:
    """``__version__`` from a zipapp payload, or None when it is not one."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if _ENTRY_MEMBER not in archive.namelist():
                return None
            source = archive.read(_ENTRY_MEMBER)
    except (zipfile.BadZipFile, OSError):
        return None
    match = _VERSION_RE.search(source)
    return match.group(1).decode("utf-8", "replace") if match else None


def payload_version(body: bytes) -> str:
    """The version a payload declares, in either shape. "" when it declares none."""
    version = _zip_version(body)
    if version:
        return version
    match = _LEGACY_VERSION_RE.search(body)
    return match.group(1).decode("utf-8", "replace") if match else ""


def published_version(ref: Optional[str] = None, timeout: float = 30.0) -> str:
    """Version of the program published for this ref. Raises SystemExit if unreachable."""
    url = _url(ref)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit("Could not reach {}: {}".format(url, exc)) from exc
    version = payload_version(body)
    if not version:
        raise SystemExit("{} does not look like {}".format(url, UPDATE_ASSET))
    return version


def running_from_checkout() -> bool:
    """True when this code sits in a source checkout rather than an install dir."""
    return install_root.running_from_checkout()


def check() -> int:
    latest = published_version()
    if version_is_newer(latest, __version__):
        print("Update available: {} -> {}".format(__version__, latest))
        print("Run: kvm-switcher update")
        return 0
    if version_is_newer(__version__, latest):
        print("Local version {} is newer than the published {}.".format(__version__, latest))
        return 0
    print("Up to date ({}).".format(__version__))
    return 0


def _download_program(ref: Optional[str] = None) -> bytes:
    """The published payload bytes."""
    return _download(ref)


def _verified_program(body: bytes, expected_newer_than: str) -> bytes:
    """The downloaded bytes, once they are known to be a working newer program.

    The pre-package single-file format: a BOM check, a version, and a compile.
    """
    # Checked first: a BOM would otherwise read as "not kvm-switcher.py", and it
    # breaks the shebang that install.sh symlinks onto PATH.
    if body.startswith(b"\xef\xbb\xbf"):
        raise SystemExit("Downloaded file starts with a BOM; nothing changed.")
    match = _LEGACY_VERSION_RE.search(body)
    if not match:
        raise SystemExit("Downloaded file is not {}; nothing changed.".format(LEGACY_PROGRAM_NAME))
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("Downloaded file is not valid UTF-8: {}".format(exc)) from exc
    # Prove it parses before it replaces a working install.
    try:
        compile(body, "<download>", "exec")
    except SyntaxError as exc:
        raise SystemExit("Downloaded file does not compile: {}".format(exc)) from exc
    downloaded = match.group(1).decode("utf-8", "replace")
    if not version_is_newer(downloaded, expected_newer_than):
        raise SystemExit(
            "Downloaded version {} is not newer than {}; nothing changed.".format(
                downloaded, expected_newer_than
            )
        )
    return body


def _verified_zip(body: bytes, expected_newer_than: str) -> bytes:
    """The payload, once it is known to be a complete, newer, compiling zipapp.

    Every module is compiled, not just the metadata: a payload whose package
    fails to import would otherwise replace a working install and then fail at
    every launch, including the launch that was meant to repair it.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, OSError) as exc:
        raise SystemExit("Downloaded file is not a {}: {}".format(UPDATE_ASSET, exc)) from exc
    with archive:
        names = archive.namelist()
        if _ENTRY_MEMBER not in names:
            raise SystemExit(
                "Downloaded file is not {}: it has no {}; nothing changed.".format(
                    UPDATE_ASSET, _ENTRY_MEMBER
                )
            )
        version = None
        compiled = 0
        for name in names:
            if not name.endswith(".py"):
                continue
            try:
                source = archive.read(name)
            except (zipfile.BadZipFile, OSError) as exc:
                raise SystemExit("Downloaded file is damaged ({}): {}".format(name, exc)) from exc
            try:
                compile(source, name, "exec")
            except SyntaxError as exc:
                raise SystemExit(
                    "Downloaded file does not compile ({}): {}".format(name, exc)
                ) from exc
            compiled += 1
            if name == _ENTRY_MEMBER:
                match = _VERSION_RE.search(source)
                if match:
                    version = match.group(1).decode("utf-8", "replace")
        if not compiled:
            raise SystemExit("Downloaded file contains no modules; nothing changed.")
        if not version:
            raise SystemExit(
                "Downloaded file does not declare __version__ in {}; nothing changed.".format(
                    _ENTRY_MEMBER
                )
            )
    if not version_is_newer(version, expected_newer_than):
        raise SystemExit(
            "Downloaded version {} is not newer than {}; nothing changed.".format(
                version, expected_newer_than
            )
        )
    return body


def _verified_payload(body: bytes, expected_newer_than: str) -> bytes:
    """Verify a download that may be a zipapp or a legacy single file."""
    if is_zip_payload(body):
        return _verified_zip(body, expected_newer_than)
    return _verified_program(body, expected_newer_than)


def _running_watcher() -> str:
    """Command line of the watcher this user started, or "" when none is running."""
    from kvm_switcher.process import running_watcher_target

    return running_watcher_target()


def _owns(target: Path, watcher: str) -> bool:
    """True when the watcher runs this very file.

    A copy installed elsewhere must never be stopped by an update in another
    install, so ownership is decided by program path, not by presence.
    """
    return bool(watcher) and same_program(watcher, str(target))


def _stop_owned_watcher() -> bool:
    """Stop the watcher that runs this install. True when something was stopped."""
    from kvm_switcher.process import stop_watcher_process

    return stop_watcher_process()


def _restart_owned_watcher() -> bool:
    """Stop and respawn the watcher for this install. True when it happened."""
    _stop_owned_watcher()
    spawn_watcher(install_root.program_argv())
    return True


def _apply_legacy(
    body: bytes, target: Path, latest: str, watcher: str, restart: bool
) -> int:
    """Replace the single file in place. Safe, because a .py is read once.

    A running ``.py`` cannot be overwritten on Windows, so the bytes land beside
    it and are swapped in. A watcher running it has to stop first, or the swap
    fails on a locked file; it is started again afterwards, on the new code.
    """
    if restart:
        _stop_owned_watcher()
    replacement = target.with_name(target.name + ".new")
    try:
        replacement.write_bytes(body)
        shutil.copymode(target, replacement)
        os.replace(replacement, target)
    except OSError as exc:
        try:
            replacement.unlink()
        except OSError:
            pass
        raise SystemExit("Could not replace {}: {}".format(target, exc)) from exc

    print("Updated {} {} -> {}".format(target, __version__, latest))
    print("The new version is live on the next command you run.")
    if restart:
        spawn_watcher(install_root.program_argv())
        print("Watcher restarted on {}.".format(latest))
    elif watcher:
        print("A different watcher is running; restart it to use the new version.")
    return 0


def _stage_zip(body: bytes, target: Path, latest: str, watcher: str) -> int:
    """Write the new archive beside the program for the next launch to apply."""
    staged = promote.pending_path(target)
    replacement = staged.with_name(staged.name + ".part")
    try:
        replacement.write_bytes(body)
        shutil.copymode(target, replacement)
        os.replace(replacement, staged)
    except OSError as exc:
        try:
            replacement.unlink()
        except OSError:
            pass
        raise SystemExit("Could not stage the update beside {}: {}".format(target, exc)) from exc

    print("Downloaded {} -> {}".format(__version__, latest))
    print("Staged {}".format(staged))
    if not _owns(target, watcher):
        # Nothing applies the staged file on our behalf, so say exactly when it
        # lands rather than implying the running program changed.
        if watcher:
            print("A different watcher is running; leave it alone.")
        print("The new version takes effect on the next launch, not now.")
        return 0

    # The watcher is ours, so restarting it applies the update: the new process
    # promotes the staged file before it imports anything.
    _restart_owned_watcher()
    print("Restarted the watcher, so {} is live now.".format(latest))
    return 0


def update() -> int:
    """Replace this program with the published version, then restart the watcher."""
    target = install_root.program_path()
    if running_from_checkout():
        raise SystemExit(
            "This is a source checkout ({}). Update it with git instead: git pull".format(
                install_root.checkout_root()
            )
        )
    latest = published_version()
    if not version_is_newer(latest, __version__):
        print("Already up to date ({}).".format(__version__))
        return 0
    if target is None:
        raise SystemExit(
            "This install has no single program file to replace. "
            "Re-run the installer to update it."
        )

    legacy_install = not install_root.is_zipapp() and target.name == LEGACY_PROGRAM_NAME
    body = _verified_payload(_download_program(), __version__)

    if legacy_install and is_zip_payload(body):
        # The installed program is the old single file. Writing a zipapp over it
        # would leave a file whose name lies about its contents, and both the
        # Windows shim and the login entry name that path explicitly.
        raise SystemExit(
            "This install is the old single-file program ({}).\n"
            "Re-run the installer once to move to the packaged version; "
            "updates work normally after that.".format(target)
        )
    if not legacy_install and not is_zip_payload(body):
        raise SystemExit(
            "Published file is a legacy single file, not {}; nothing changed.".format(
                UPDATE_ASSET
            )
        )

    watcher = _running_watcher()
    owns = _owns(target, watcher)
    if legacy_install:
        return _apply_legacy(body, target, latest, watcher, restart=owns)
    return _stage_zip(body, target, latest, watcher)
