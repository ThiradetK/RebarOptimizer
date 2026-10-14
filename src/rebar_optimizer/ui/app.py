"""GUI entrypoint: logging, QApplication, theme, crash handler (6.3, 6.5)."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from PySide6.QtWidgets import QApplication, QMessageBox

from rebar_optimizer import __version__
from rebar_optimizer.i18n.th import GUI_TEXT
from rebar_optimizer.logging_config import setup_logging
from rebar_optimizer.ui.main_window import MainWindow
from rebar_optimizer.ui.theme import apply_theme

logger = logging.getLogger(__name__)


def install_excepthook(log_path: Path) -> None:
    """Unexpected exceptions: full stack to the log, short Thai message on screen."""

    def handle(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        if (
            QApplication.instance() is not None
            and threading.current_thread() is threading.main_thread()
        ):
            QMessageBox.critical(
                None,
                GUI_TEXT["unexpected_title"],
                GUI_TEXT["unexpected"].format(log_path=log_path),
            )

    sys.excepthook = handle

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value or args.exc_type(), args.exc_traceback),
        )

    threading.excepthook = thread_hook


def main(argv: Sequence[str] | None = None) -> int:
    log_path = setup_logging()
    logger.info("rebar_optimizer %s gui starting", __version__)
    install_excepthook(log_path)
    app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName("Rebar Optimizer")
    app.setApplicationVersion(__version__)
    apply_theme(app)
    window = MainWindow(log_path=log_path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
