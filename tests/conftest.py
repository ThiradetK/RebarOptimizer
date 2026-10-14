"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import openpyxl
import pytest

DEFAULT_HEADERS = ("Size", "No", "Length", "Quantity")

MakeXlsx = Callable[..., Path]


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
