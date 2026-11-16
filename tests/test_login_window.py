"""The login entry must be windowless on Windows.

A console interpreter launched at logon gets its own terminal panel, and closing
that panel terminates the watcher. These tests pin the interpreter choice without
depending on the machine's Python layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kvm_switcher import install_root


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows interpreter layout")
def test_background_interpreter_is_pythonw():
    executable = Path(install_root._background_executable())
    assert executable.name.lower() == "pythonw.exe"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows interpreter layout")
def test_background_executable_exists():
    # A missing file would make the login entry fail silently at logon.
    assert Path(install_root._background_executable()).is_file()


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows interpreter layout")
def test_background_argv_differs_from_foreground():
    foreground = install_root.program_argv()
    background = install_root.program_argv(background=True)
    assert Path(foreground[0]).name.lower() == "python.exe"
    assert Path(background[0]).name.lower() == "pythonw.exe"
    # Same program, only the interpreter differs.
    assert foreground[1:] == background[1:]


def test_background_falls_back_when_pythonw_is_missing(monkeypatch, tmp_path):
    """A layout without pythonw.exe must still produce a runnable command."""
    if not sys.platform.startswith("win"):
        pytest.skip("only Windows has a separate windowless interpreter")
    # Point at a directory with no pythonw.exe beside the interpreter.
    monkeypatch.setattr(install_root.sys, "executable", str(tmp_path / "python.exe"))
    assert install_root._background_executable() == str(tmp_path / "python.exe")


def test_non_windows_never_swaps_the_interpreter(monkeypatch):
    monkeypatch.setattr(install_root.sys, "platform", "linux")
    monkeypatch.setattr(install_root.sys, "executable", "/usr/bin/python3")
    assert install_root._background_executable() == "/usr/bin/python3"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="registry entry is Windows-only")
def test_login_entry_names_the_windowless_interpreter():
    """The Run-key command is what actually starts at logon."""
    from kvm_switcher.service import windows as win_service

    entry = win_service.command()
    assert "pythonw.exe" in entry.lower(), entry
