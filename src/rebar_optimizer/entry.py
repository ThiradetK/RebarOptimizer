"""Entry point of the packaged executable (6.4, 7).

``RebarOptimizer.exe`` opens the GUI. ``RebarOptimizer.exe --selftest`` solves
a built-in problem without creating a ``QApplication``, writes the result to
the log and returns the exit code (used by CI on the built exe).
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import time
from collections.abc import Sequence


def _ensure_streams() -> None:
    """A windowed (no console) exe has ``sys.stdout is None``; printing would fail."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - process lifetime
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - process lifetime


def main(argv: Sequence[str] | None = None) -> int:
    started = time.perf_counter()
    args = list(sys.argv[1:] if argv is None else argv)
    _ensure_streams()
    # In the packaged exe a size worker (services.parallel) is this same program
    # started again: this call runs the worker and exits before any GUI exists.
    multiprocessing.freeze_support()
    if "--selftest" in args:
        from rebar_optimizer.cli import main as cli_main  # noqa: PLC0415 - keep Qt out

        return cli_main(["--selftest"])
    from rebar_optimizer.ui.app import main as gui_main  # noqa: PLC0415 - heavy import

    return gui_main([sys.argv[0], *args], started=started)


if __name__ == "__main__":
    sys.exit(main())
