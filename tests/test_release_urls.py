"""The documented install commands must come from the newest release.

A user-facing one-liner that reads a branch is unreviewed code running in their
shell, so these tests pin the URL shape rather than trusting the README prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = "https://github.com/algun/kvm-switcher/releases/latest/download"


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_installer_never_reads_a_branch(script):
    text = read(script)
    assert "raw.githubusercontent.com" not in text, (
        "{} fetches from a branch, so an unreleased commit could reach a user"
        .format(script)
    )
    assert not re.search(r'\bref\s*=\s*"main"', text), (
        "{} still pins the main branch".format(script)
    )


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_installer_fetches_from_the_latest_release(script):
    text = read(script)
    assert "releases/latest/download" in text, (
        "{} does not use the latest release".format(script)
    )


def test_readme_one_liners_use_the_release_url():
    text = read("README.md")
    # The piped commands users copy must resolve against a release, not a branch.
    for line in text.splitlines():
        if "install.sh | bash" in line or "install.ps1 | iex" in line:
            assert RELEASE in line, line
            assert "raw.githubusercontent.com" not in line, line


def test_readme_no_longer_warns_about_a_moving_branch():
    # The warning existed because installers read main; that is no longer true.
    text = read("README.md")
    assert "is not gated on a release" not in text
    assert "broken commit" not in text


def test_release_publishes_the_installer_assets():
    """The release-link one-liner 404s unless these are attached to the release."""
    workflow = read(Path(".github") / "workflows" / "release.yml")
    for asset in ("install.sh", "install.ps1", "config.json", "kvm-switcher.pyz"):
        assert asset in workflow, "release.yml does not publish {}".format(asset)
