"""Tests for the zipapp delivery: the built artifact, staged updates, the swap.

These are separate from the characterisation tests because they pin Stage 2's
new behaviour rather than the pre-split program's. The two facts they exist to
protect:

* an update is staged beside the program and applied by a process that has not
  imported the package yet, and
* a running archive is never swapped in place, because zipimport's cached table
  of contents survives the rename and then reads the wrong bytes.

The second is not a theory: ``test_replacing_the_running_archive_would_...``
documents the failure the probe produced.
"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kvm_switcher import promote, update

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "kvm-switcher.pyz"


# --------------------------------------------------------------------------
# Build helpers
# --------------------------------------------------------------------------


def make_archive(version: str = "9.9.9", extra_modules=None, pad: int = 0) -> bytes:
    """A minimal zipapp payload shaped like the real one."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", "raise SystemExit(0)\n")
        init = '__version__ = "{}"\n'.format(version).encode("utf-8")
        archive.writestr("kvm_switcher/__init__.py", init)
        module = "def run():\n    return 1\n"
        if pad:
            module += "# padding\n" + 'PAD = "' + "x" * pad + '"\n'
        archive.writestr("kvm_switcher/cli.py", module)
        for name, source in (extra_modules or {}).items():
            archive.writestr(name, source)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Payload verification
# --------------------------------------------------------------------------


def test_a_good_zip_payload_is_accepted():
    body = make_archive("9.9.9")
    assert update._verified_payload(body, "0.1.6") == body


def test_a_zip_payload_is_detected_as_a_zip():
    assert update.is_zip_payload(make_archive("9.9.9"))
    assert not update.is_zip_payload(b'VERSION = "9.9.9"\n')


@pytest.mark.parametrize(
    "body, message",
    [
        # Not newer: the whole point of the check.
        (make_archive("0.0.1"), "not newer"),
        # A truncated download is not a readable archive.
        (make_archive("9.9.9")[:60], "not a kvm-switcher.pyz"),
    ],
)
def test_a_bad_zip_payload_is_rejected(body, message):
    with pytest.raises(SystemExit) as caught:
        update._verified_zip(body, "0.1.6")
    assert message in str(caught.value)


def test_a_zip_without_the_package_member_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", "raise SystemExit(0)\n")
        archive.writestr("something_else.py", "__version__ = '9.9.9'\n")
    with pytest.raises(SystemExit) as caught:
        update._verified_payload(buffer.getvalue(), "0.1.6")
    assert "no kvm_switcher/__init__.py" in str(caught.value)


def test_every_module_is_compiled_not_just_the_metadata():
    """A payload that would fail at import must not replace a working install."""
    body = make_archive("9.9.9", extra_modules={"kvm_switcher/broken.py": "def broken(:\n"})
    with pytest.raises(SystemExit) as caught:
        update._verified_payload(body, "0.1.6")
    assert "does not compile" in str(caught.value)
    assert "kvm_switcher/broken.py" in str(caught.value)


def test_a_zip_without_a_version_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("kvm_switcher/__init__.py", "VERSION = '9.9.9'\n")
    with pytest.raises(SystemExit) as caught:
        update._verified_payload(buffer.getvalue(), "0.1.6")
    assert "__version__" in str(caught.value)


def test_the_payload_version_is_read_from_either_shape():
    assert update.payload_version(make_archive("9.9.9")) == "9.9.9"
    assert update.payload_version(b'VERSION = "8.8.8"\n') == "8.8.8"
    assert update.payload_version(b"<html>404</html>") == ""


# --------------------------------------------------------------------------
# Legacy installs
# --------------------------------------------------------------------------


def test_a_legacy_install_refuses_a_zipapp_and_says_reinstall(tmp_path, monkeypatch):
    """A .py install must not silently become a .pyz under the same name."""
    target = tmp_path / "kvm-switcher.py"
    target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: make_archive("9.9.9"))
    monkeypatch.setattr(update.install_root, "program_path", lambda: target)
    monkeypatch.setattr(update.install_root, "is_zipapp", lambda: False)

    with pytest.raises(SystemExit) as caught:
        update.update()
    message = str(caught.value)
    assert "Re-run the installer" in message
    # Nothing was written: the old file is untouched and no .new appeared.
    assert b"0.1.0" in target.read_bytes()
    assert not (tmp_path / "kvm-switcher.py.new").exists()


def test_a_legacy_install_still_takes_a_legacy_payload(tmp_path, monkeypatch, capsys):
    target = tmp_path / "kvm-switcher.py"
    target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
    body = b'VERSION = "9.9.9"\n'
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: body)
    monkeypatch.setattr(update.install_root, "program_path", lambda: target)
    monkeypatch.setattr(update.install_root, "is_zipapp", lambda: False)
    from kvm_switcher import process

    monkeypatch.setattr(process, "running_watcher_target", lambda: "")

    assert update.update() == 0
    assert b"9.9.9" in target.read_bytes()
    assert "live on the next command" in capsys.readouterr().out


def test_a_legacy_update_stops_the_watcher_before_swapping_the_file(tmp_path, monkeypatch):
    """A running .py is locked on Windows, so the stop has to come first."""
    order = []
    target = tmp_path / "kvm-switcher.py"
    target.write_text('VERSION = "0.1.0"\n', encoding="utf-8")
    body = b'VERSION = "9.9.9"\n'
    real_replace = update.os.replace

    def tracking_replace(source, destination):
        order.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(update.os, "replace", tracking_replace)
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: body)
    monkeypatch.setattr(update.install_root, "program_path", lambda: target)
    monkeypatch.setattr(update.install_root, "is_zipapp", lambda: False)
    monkeypatch.setattr(update, "_running_watcher", lambda: str(target))
    monkeypatch.setattr(
        update, "_stop_owned_watcher", lambda: order.append("stop") or True
    )
    monkeypatch.setattr(update, "spawn_watcher", lambda _argv: order.append("spawn") or 1)

    assert update.update() == 0
    assert order == ["stop", "replace", "spawn"], order


# --------------------------------------------------------------------------
# Staging and promotion
# --------------------------------------------------------------------------


def test_a_zipapp_install_is_staged_not_swapped(tmp_path, monkeypatch, capsys):
    """The running archive must not change; the payload lands as a .new file."""
    target = tmp_path / "kvm-switcher.pyz"
    original = make_archive("0.1.0")
    target.write_bytes(original)
    body = make_archive("9.9.9")
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: body)
    monkeypatch.setattr(update.install_root, "program_path", lambda: target)
    monkeypatch.setattr(update.install_root, "is_zipapp", lambda: True)
    from kvm_switcher import process

    monkeypatch.setattr(process, "running_watcher_target", lambda: "")

    assert update.update() == 0
    # The archive in place is untouched, because it is the one running.
    assert target.read_bytes() == original
    staged = promote.pending_path(target)
    assert staged.read_bytes() == body
    assert not staged.with_name(staged.name + ".part").exists()
    text = capsys.readouterr().out
    assert "takes effect on the next launch" in text
    # And it does not claim to be live now.
    assert "live now" not in text


def test_staging_replaces_an_older_staged_update(tmp_path, monkeypatch):
    target = tmp_path / "kvm-switcher.pyz"
    target.write_bytes(make_archive("0.1.0"))
    promote.pending_path(target).write_bytes(make_archive("8.8.8"))
    body = make_archive("9.9.9")
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.setattr(update, "published_version", lambda **_kw: "9.9.9")
    monkeypatch.setattr(update, "_download_program", lambda **_kw: body)
    monkeypatch.setattr(update.install_root, "program_path", lambda: target)
    monkeypatch.setattr(update.install_root, "is_zipapp", lambda: True)
    from kvm_switcher import process

    monkeypatch.setattr(process, "running_watcher_target", lambda: "")

    assert update.update() == 0
    assert promote.pending_path(target).read_bytes() == body


def test_promote_is_a_no_op_without_a_staged_file(tmp_path, monkeypatch):
    target = tmp_path / "kvm-switcher.pyz"
    target.write_bytes(make_archive("0.1.0"))
    monkeypatch.setattr(promote.install_root, "archive_path", lambda: target)
    assert promote.promote_pending(["kvm-switcher"]) is None
    assert promote.is_complete(target)


def test_promote_ignores_a_directory_install(monkeypatch):
    monkeypatch.setattr(promote.install_root, "archive_path", lambda: None)
    assert promote.promote_pending(["kvm-switcher"]) is None


def test_promote_discards_an_incomplete_staged_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "kvm-switcher.pyz"
    target.write_bytes(make_archive("0.1.0"))
    staged = promote.pending_path(target)
    staged.write_bytes(b"truncated download, not a zip")
    monkeypatch.setattr(promote.install_root, "archive_path", lambda: target)

    assert promote.promote_pending(["kvm-switcher"]) is None
    assert not staged.exists()
    assert "Discarded an incomplete update" in capsys.readouterr().err
    # The working program is untouched.
    assert target.read_bytes() == make_archive("0.1.0")


def test_promote_keeps_the_staged_file_when_the_swap_is_blocked(tmp_path, monkeypatch, capsys):
    """A locked archive (another process) must not lose the downloaded payload."""
    target = tmp_path / "kvm-switcher.pyz"
    target.write_bytes(make_archive("0.1.0"))
    staged = promote.pending_path(target)
    body = make_archive("9.9.9")
    staged.write_bytes(body)
    monkeypatch.setattr(promote.install_root, "archive_path", lambda: target)
    # os.replace is imported into promote's namespace, so patch there.
    import errno

    def refuse(_source, _target):
        raise OSError(errno.EACCES, "in use by another process")

    monkeypatch.setattr(promote.os, "replace", refuse)

    assert promote.promote_pending(["kvm-switcher"]) is None
    assert staged.read_bytes() == body
    assert "Could not apply the update yet" in capsys.readouterr().err


def test_promote_swaps_and_relaunches(tmp_path, monkeypatch):
    """After the swap, a fresh interpreter runs the new payload; this one does not."""
    target = tmp_path / "kvm-switcher.pyz"
    target.write_bytes(make_archive("0.1.0"))
    body = make_archive("9.9.9", pad=2000)
    promote.pending_path(target).write_bytes(body)
    monkeypatch.setattr(promote.install_root, "archive_path", lambda: target)
    monkeypatch.setattr(promote, "_relaunch", lambda _target, _argv: 7)

    assert promote.promote_pending(["kvm-switcher", "version"]) == 7
    assert target.read_bytes() == body
    assert not promote.pending_path(target).exists()


# --------------------------------------------------------------------------
# The hazard the design exists for
# --------------------------------------------------------------------------


def test_replacing_the_running_archive_would_break_later_imports(tmp_path):
    """Pin the probe's finding, so the deferral is not "unnecessary" later.

    A fresh process replaces its own archive, then imports a module that exists
    in both payloads at different offsets. zipimport reads by the stale offset
    and fails, which is why promotion is deferred to a new interpreter.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "first.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    (source / "second.py").write_text("VALUE = 'old-second'\n", encoding="utf-8")
    program = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import first\n"
        "archive = Path(sys.argv[0]).resolve()\n"
        "os.replace(str(archive) + '.new', str(archive))\n"
        "try:\n"
        "    import second\n"
        "except BaseException as exc:\n"
        "    print(type(exc).__name__)\n"
        "    raise SystemExit(3)\n"
        "print(second.VALUE)\n"
    )
    (source / "app.py").write_text(program, encoding="utf-8")
    archive = tmp_path / "app.pyz"

    import zipapp

    zipapp.create_archive(source, target=archive, main="app:main")

    # The replacement: same module names, different offsets.
    staging = tmp_path / "next"
    staging.mkdir()
    (staging / "first.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    (staging / "second.py").write_text(
        "# a longer module, so every later offset moves\n" + "VALUE = 'new-second'\n" * 200,
        encoding="utf-8",
    )
    (staging / "app.py").write_text(program, encoding="utf-8")
    replacement = tmp_path / "next.pyz"
    zipapp.create_archive(staging, target=replacement, main="app:main")
    archive.with_name(archive.name + ".new").write_bytes(replacement.read_bytes())

    completed = subprocess.run(
        [sys.executable, str(archive)], text=True, capture_output=True, timeout=120
    )
    # Either the stale read fails loudly or it silently yields the old module;
    # both mean the same thing: the running process cannot continue on the new
    # archive. What must never happen is reading the new module.
    assert completed.returncode != 0 or completed.stdout.strip() == "old-second"
    assert "new-second" not in completed.stdout


# --------------------------------------------------------------------------
# The real artifact
# --------------------------------------------------------------------------


@pytest.mark.skipif(not DIST.is_file(), reason="dist/kvm-switcher.pyz not built")
def test_the_built_zipapp_runs_and_reports_its_version():
    from kvm_switcher import __version__

    completed = subprocess.run(
        [sys.executable, str(DIST), "version"], text=True, capture_output=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    reported = completed.stdout.strip()
    assert reported == __version__, (
        "dist/kvm-switcher.pyz reports {} but the source is {}. The artifact is stale: "
        "rebuild with `python build.py` before running the tests.".format(reported, __version__)
    )


@pytest.mark.skipif(not DIST.is_file(), reason="dist/kvm-switcher.pyz not built")
def test_the_built_zipapp_carries_the_package():
    with zipfile.ZipFile(DIST) as archive:
        names = set(archive.namelist())
    assert "__main__.py" in names
    assert "kvm_switcher/__init__.py" in names
    assert "kvm_switcher/cli.py" in names
    # Promotion has to run before the CLI is imported, so it must be present.
    assert "kvm_switcher/promote.py" in names
    # No build residue.
    assert not [name for name in names if "__pycache__" in name]
    assert not [name for name in names if name.endswith(".pyc")]


def test_the_archive_starts_with_its_shebang():
    """A POSIX install runs the file directly, so the shebang has to lead."""
    if not DIST.is_file():
        pytest.skip("dist/kvm-switcher.pyz not built")
    with DIST.open("rb") as handle:
        first = handle.readline()
    assert first == b"#!/usr/bin/env python3\n"
