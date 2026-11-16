"""`uninstall` removal: what each flag and answer actually deletes.

The destructive paths are tested against a temp HOME so nothing here can touch
the real config, launcher, or install directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kvm_switcher import paths, purge


@pytest.fixture()
def scratch_config(tmp_path, monkeypatch):
    """A config file inside its own directory, so the empty-dir rule is visible."""
    folder = tmp_path / "cfg"
    folder.mkdir()
    config = folder / "config.json"
    config.write_text(json.dumps({"devices": [], "display": {"input": "0x0f"}}), encoding="utf-8")
    monkeypatch.setenv("KVM_CONFIG", str(config))
    return config


def test_settings_removal_deletes_config_and_its_directory(scratch_config):
    removed, failed = purge.remove_settings()
    assert failed == []
    assert not scratch_config.exists()
    assert not scratch_config.parent.exists()
    assert str(scratch_config) in removed


def test_settings_removal_takes_the_pid_file(scratch_config):
    pid = scratch_config.with_name("watcher.pid")
    pid.write_text("4242\n", encoding="utf-8")
    purge.remove_settings()
    assert not pid.exists()
    assert not scratch_config.parent.exists()


def test_settings_removal_keeps_a_shared_directory(scratch_config):
    # KVM_CONFIG can point into a directory holding unrelated files; those must
    # survive, so the directory is only removed when it ends up empty. The
    # assertion is on the directory, not just the files: an unconditional
    # rmtree would delete the unrelated file and this must catch that.
    other = scratch_config.with_name("unrelated.txt")
    other.write_text("keep me", encoding="utf-8")
    removed, failed = purge.remove_settings()
    assert not scratch_config.exists()
    assert other.is_file(), "an unrelated file in a shared directory was deleted"
    assert scratch_config.parent.is_dir()
    assert failed == []
    assert str(scratch_config.parent) not in removed


def test_legacy_devices_txt_is_removed_too(scratch_config):
    legacy = scratch_config.with_name("devices.txt")
    legacy.write_text("1234:abcd\n", encoding="utf-8")
    purge.remove_settings()
    assert not legacy.exists()


def test_program_removal_refuses_in_a_checkout(monkeypatch):
    # Deleting the repository would destroy the working tree, so a checkout is
    # left alone and the caller is told to use git.
    monkeypatch.setattr(purge.install_root, "running_from_checkout", lambda: True)
    removed, failed = purge.remove_program()
    assert removed == []
    assert failed == []


def test_program_removal_deletes_launcher_and_install_dir(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    (install / "kvm-switcher.pyz").write_bytes(b"not a real archive")
    launcher = tmp_path / "bin" / "kvm-switcher"
    launcher.parent.mkdir()
    # The launcher must actually name this install, or removal refuses.
    launcher.write_text(
        '#!/bin/sh\nexec python "{}/kvm-switcher.pyz" "$@"\n'.format(install),
        encoding="utf-8",
    )

    monkeypatch.setattr(purge.install_root, "running_from_checkout", lambda: False)
    monkeypatch.setattr(purge.install_root, "install_dir", lambda: install)
    monkeypatch.setattr(purge.paths, "launcher_path", lambda: launcher)

    removed, failed = purge.remove_program()
    assert failed == []
    assert not launcher.exists()
    assert not install.exists()
    assert str(launcher) in removed and str(install) in removed


def test_program_removal_leaves_a_launcher_that_points_elsewhere(tmp_path, monkeypatch):
    # Regression: running a copy from a temporary directory must not delete the
    # launcher belonging to the real installation.
    install = tmp_path / "temp-copy"
    install.mkdir()
    (install / "kvm-switcher.pyz").write_bytes(b"copy")
    real = tmp_path / "real-install"
    launcher = tmp_path / "bin" / "kvm-switcher"
    launcher.parent.mkdir()
    launcher.write_text(
        '#!/bin/sh\nexec python "{}/kvm-switcher.pyz" "$@"\n'.format(real),
        encoding="utf-8",
    )

    monkeypatch.setattr(purge.install_root, "running_from_checkout", lambda: False)
    monkeypatch.setattr(purge.install_root, "install_dir", lambda: install)
    monkeypatch.setattr(purge.paths, "launcher_path", lambda: launcher)

    removed, _failed = purge.remove_program()
    assert launcher.is_file(), "the real installation's launcher was deleted"
    assert not install.exists()
    assert str(launcher) not in removed


def test_windows_shim_is_matched_by_its_install_path(tmp_path, monkeypatch):
    install = tmp_path / "Local" / "kvm-switcher"
    install.mkdir(parents=True)
    launcher = tmp_path / "bin" / "kvm-switcher.cmd"
    launcher.parent.mkdir()
    launcher.write_text(
        '@echo off\r\npython "{}\\kvm-switcher.pyz" %*\r\n'.format(install),
        encoding="utf-8",
    )
    assert purge._launcher_targets_this_install(launcher, install)
    assert not purge._launcher_targets_this_install(launcher, tmp_path / "other")


def test_launcher_path_matches_what_the_installers_write(monkeypatch):
    monkeypatch.setattr(paths, "platform_key", lambda: "windows")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\someone")
    assert str(paths.launcher_path()).replace("/", "\\").endswith(
        r"bin\kvm-switcher.cmd"
    )

    monkeypatch.setattr(paths, "platform_key", lambda: "linux")
    assert paths.launcher_path().parts[-2:] == ("bin", "kvm-switcher")


class TestAskYesNo:
    """The prompt must never block a scripted run, and must default to no."""

    @staticmethod
    def _tty(monkeypatch, answer):
        class Tty:
            @staticmethod
            def isatty():
                return True

        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return answer

        monkeypatch.setattr(sys, "stdin", Tty())
        monkeypatch.setattr(sys, "stdout", Tty())
        monkeypatch.setattr("builtins.input", fake_input)
        return prompts

    def test_yes_answers(self, monkeypatch):
        from kvm_switcher.cli import ask_yes_no

        for answer in ("y", "Y", "yes", "YES", " y "):
            self._tty(monkeypatch, answer)
            assert ask_yes_no("q?") is True, answer

    def test_no_answers_and_empty_default(self, monkeypatch):
        from kvm_switcher.cli import ask_yes_no

        for answer in ("n", "N", "no", ""):
            self._tty(monkeypatch, answer)
            assert ask_yes_no("q?") is False, answer

    def test_prompt_shows_the_default(self, monkeypatch):
        from kvm_switcher.cli import ask_yes_no

        prompts = self._tty(monkeypatch, "n")
        ask_yes_no("Remove your saved settings too?")
        assert prompts == ["Remove your saved settings too? [y/N] "]

    def test_no_terminal_returns_default_without_prompting(self, monkeypatch):
        from kvm_switcher.cli import ask_yes_no

        class NoTty:
            @staticmethod
            def isatty():
                return False

        def boom(*_a, **_k):
            raise AssertionError("must not prompt without a terminal")

        monkeypatch.setattr(sys, "stdin", NoTty())
        monkeypatch.setattr(sys, "stdout", NoTty())
        monkeypatch.setattr("builtins.input", boom)
        assert ask_yes_no("q?") is False
        assert ask_yes_no("q?", default=True) is True
