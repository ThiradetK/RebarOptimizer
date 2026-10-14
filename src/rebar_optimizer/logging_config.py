"""Logging setup. Called only from entrypoints (``cli.main``, ``ui.app``), never on import."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR_NAME = "RebarOptimizer"
LOG_FILE_NAME = "rebar_optimizer.log"
LOG_DIR_ENV = "REBAR_OPTIMIZER_LOG_DIR"
_HANDLER_NAME = "rebar_optimizer_file"


def default_log_dir() -> Path:
    """``%LOCALAPPDATA%\\RebarOptimizer\\logs`` (overridable via ``REBAR_OPTIMIZER_LOG_DIR``)."""
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME / "logs"


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> Path:
    """Attach a rotating UTF-8 file handler to the root logger; return the log path.

    Idempotent: a second call with the same directory is a no-op.
    """
    directory = log_dir or default_log_dir()
    path = directory / LOG_FILE_NAME
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            if Path(getattr(handler, "baseFilename", "")) == path.resolve():
                return path
            root.removeHandler(handler)
            handler.close()
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    return path
