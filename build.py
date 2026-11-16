#!/usr/bin/env python3
"""Build the single-file zipapp, ``dist/kvm-switcher.pyz``.

The archive is the shipped program: installers put it in the install directory
and the login service runs it. It is built from a source tree rather than an
installed package so the build needs nothing beyond the standard library.

Reproducibility: entries are written in sorted order with a fixed timestamp and
no ``__pycache__``, so building the same sources twice produces byte-identical
output. That lets a release verify a rebuild rather than trusting it.

Usage:
    python build.py                  # writes dist/kvm-switcher.pyz
    python build.py --output X.pyz   # writes somewhere else
    python build.py --expect-version 0.2.2
                                     # exit 1 unless the built version matches,
                                     # which is the release tag check

The archive layout:

    __main__.py            promote a staged update, then run the CLI
    kvm_switcher/...       the package, unchanged
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
PACKAGE = SRC / "kvm_switcher"
DEFAULT_OUTPUT = ROOT / "dist" / "kvm-switcher.pyz"

# The interpreter line the archive carries, so `./kvm-switcher.pyz` works on a
# POSIX system once the file is executable.
SHEBANG = "#!/usr/bin/env python3\n"

# Fixed timestamp (1980-01-01, the earliest a zip can express) keeps two builds
# of the same sources identical.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

# The entry point. `kvm_switcher/__main__.py` already does the right thing for an
# archive - it promotes a staged update before importing the CLI - so it is
# shipped as `__main__.py` rather than duplicated here. One copy cannot drift.
_MAIN_SOURCE = PACKAGE / "__main__.py"


def source_version() -> str:
    """``kvm_switcher.__version__`` read from the source tree."""
    sys.path.insert(0, str(SRC))
    try:
        from kvm_switcher import __version__

        return __version__
    finally:
        sys.path.pop(0)


def _module_sources() -> List[Tuple[str, Path]]:
    """Every module to ship, as ``(archive name, path)`` in sorted order."""
    if not (PACKAGE / "__init__.py").is_file():
        raise SystemExit("No package at {}; run from the repository root.".format(PACKAGE))
    found: List[Tuple[str, Path]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        found.append((path.relative_to(SRC).as_posix(), path))
    if not found:
        raise SystemExit("No modules found under {}.".format(PACKAGE))
    return found


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = mode << 16
    archive.writestr(info, data)


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    """Write the zipapp to ``output`` and return the path."""
    modules = _module_sources()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            # 0o644 for modules, 0o755 for the entry point, so the archive is
            # executable when installed on a POSIX system.
            _write_entry(archive, "__main__.py", _MAIN_SOURCE.read_bytes(), 0o755)
            for name, path in modules:
                if name == "kvm_switcher/__main__.py":
                    # Shipped as the archive entry point instead; the package
                    # copy would be dead weight.
                    continue
                _write_entry(archive, name, path.read_bytes(), 0o644)
        # The shebang has to be the first bytes of the file itself; a zip keeps
        # its directory at the end, so prefixing it here does not corrupt it.
        with temporary.open("rb") as handle:
            body = handle.read()
        temporary.write_bytes(SHEBANG.encode("ascii") + body)
        os.replace(str(temporary), str(output))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def verify(path: Path, expected_version: Optional[str] = None) -> str:
    """Check a built archive opens, ships the package, and reports its version.

    Importing the version out of the archive rather than the build process means
    the check sees what a user would actually run.
    """
    if not zipfile.is_zipfile(path):
        raise SystemExit("{} is not a zip archive".format(path))
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for required in ("__main__.py", "kvm_switcher/__init__.py"):
            if required not in names:
                raise SystemExit("{} is missing {}".format(path, required))
        namespace = {"__name__": "kvm_switcher.__init__", "__file__": "kvm_switcher/__init__.py"}
        try:
            exec(compile(archive.read("kvm_switcher/__init__.py"), "kvm_switcher/__init__.py", "exec"), namespace)
        except SyntaxError as exc:
            raise SystemExit("{} has a broken __init__.py: {}".format(path, exc)) from exc
        version = namespace.get("__version__")
        if not version:
            raise SystemExit("{} does not define __version__".format(path))
    if expected_version is not None and version != expected_version:
        raise SystemExit(
            "Built version {} does not match {}; the tag and the source disagree.".format(
                version, expected_version
            )
        )
    return version


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--expect-version",
        default=None,
        help="fail unless the built program reports this version (the release tag)",
    )
    arguments = parser.parse_args(argv)
    output = build(arguments.output)
    version = verify(output, arguments.expect_version)
    size = output.stat().st_size
    print("Built {} (version {}, {} bytes)".format(output, version, size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
