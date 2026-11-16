"""Entry point for ``python -m kvm_switcher``.

Delegates to :func:`kvm_switcher.cli.main`, the same function a console
script or zipapp would call.

A staged update is applied before the CLI runs, because replacing the archive
invalidates zipimport's cached table of contents for the rest of the process.
Nothing beyond this module may be imported from the archive beforehand.
"""

import sys


def _promote():
    """Apply a staged ``.new`` archive, or None when there is nothing to do."""
    try:
        from kvm_switcher.promote import promote_pending
    except ImportError:
        # A directory install predating promote.py: nothing can be staged.
        return None
    return promote_pending(sys.argv)


if __name__ == "__main__":
    _promoted = _promote()
    if _promoted is not None:
        # The update was applied; a fresh interpreter already ran the new code.
        sys.exit(_promoted)
    from kvm_switcher.cli import main

    sys.exit(main(sys.argv))
