"""Shared pytest fixtures."""

from __future__ import annotations

import os

# GUI tests run headless; must be set before any QApplication exists.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Callable, Sequence
from pathlib import Path

import openpyxl
import pytest

from rebar_optimizer.logging_config import LOG_DIR_ENV
from rebar_optimizer.ui.user_settings import SETTINGS_ENV

DEFAULT_HEADERS = ("Size", "No", "Length", "Quantity")

MakeXlsx = Callable[..., Path]


@pytest.fixture(autouse=True)
def _isolated_log_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never write logs to the real %LOCALAPPDATA% during tests."""
    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path_factory.getbasetemp() / "logs"))


@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real %APPDATA% settings during tests."""
    path = tmp_path / "user_settings.json"
    monkeypatch.setenv(SETTINGS_ENV, str(path))
    return path


@pytest.fixture
def make_xlsx(tmp_path: Path) -> MakeXlsx:
    """Build an .xlsx whose first sheet holds ``headers`` then ``rows``.

    Row ``i`` of ``rows`` lands on Excel row ``i + 2``. Pass ``headers=None``
    to write ``rows`` verbatim (no header row).
    """
    counter = iter(range(1_000_000))

    def build(
        rows: Sequence[Sequence[object]],
        headers: Sequence[object] | None = DEFAULT_HEADERS,
        name: str | None = None,
    ) -> Path:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        if headers is not None:
            sheet.append(list(headers))
        for row in rows:
            sheet.append(list(row))
        path = tmp_path / (name or f"input_{next(counter)}.xlsx")
        workbook.save(path)
        return path

    return build
