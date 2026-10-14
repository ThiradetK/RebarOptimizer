"""Read the input workbook into normalized ``Piece`` objects.

openpyxl is used directly (no pandas) so that Excel row numbers and cell types
stay under our control. Every problem is collected before reporting (6.1).

Pieces come from the first sheet that is not the optional inventory sheet
(phase 8). That sheet, named ``Stock`` (or ``คงคลัง`` / ``เหล็กคงคลัง``), lists
bars already on hand: ``Size``, ``Length`` (m), ``Quantity``. Its issues carry
a ``sheet`` parameter so messages say which sheet they refer to.
"""

from __future__ import annotations

import math
import re
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from rebar_optimizer.domain.errors import InputValidationError, Issue, IssueCode
from rebar_optimizer.domain.models import InventoryStock, Piece

HEADER_ROW = 1
SUB_MM_TOLERANCE = 0.01
"""Warn when a length has more precision than 1 mm by more than this (in mm)."""
LIKELY_MM_THRESHOLD_M = 100.0
SUPPORTED_SUFFIXES = (".xlsx", ".xlsm")

_NUMBER_TEXT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_PREFIXED_SIZE = re.compile(r"^([A-Z]+)(\d+(?:\.\d+)?)$")


class Column(StrEnum):
    SIZE = "Size"
    NO = "No"
    LENGTH = "Length"
    QUANTITY = "Quantity"


PIECE_COLUMNS = (Column.SIZE, Column.NO, Column.LENGTH, Column.QUANTITY)
INVENTORY_COLUMNS = (Column.SIZE, Column.LENGTH, Column.QUANTITY)
INVENTORY_SHEET_NAMES = ("stock", "คงคลัง", "เหล็กคงคลัง")
"""Names (case-insensitive) that mark the optional inventory sheet."""

COLUMN_ALIASES: dict[str, Column] = {
    "size": Column.SIZE,
    "ขนาด": Column.SIZE,
    "no": Column.NO,
    "หมายเลข": Column.NO,
    "length": Column.LENGTH,
    "ความยาว": Column.LENGTH,
    "quantity": Column.QUANTITY,
    "จำนวน": Column.QUANTITY,
}


@dataclass(frozen=True)
class InputTable:
    """Raw copy of the input sheet (non-empty data rows only), for the output workbook."""

    headers: tuple[str, ...]
    row_numbers: tuple[int, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class ReadResult:
    path: Path
    pieces: tuple[Piece, ...]
    warnings: tuple[Issue, ...]
    table: InputTable
    inventory: tuple[InventoryStock, ...] = ()
    """Bars on hand from the inventory sheet; empty without one."""
    inventory_sheet: str | None = None
    """Title of the inventory sheet, if the workbook has one."""


@dataclass(frozen=True)
class _Sheets:
    pieces: list[tuple[object, ...]]
    inventory: list[tuple[object, ...]] | None
    inventory_title: str | None


@dataclass(frozen=True)
class _Record:
    row: int
    size: str
    no: str
    length_mm: int
    quantity: int


def read_pieces(path: str | Path) -> ReadResult:
    """Read and validate the first sheet of ``path``.

    Raises ``InputValidationError`` (with errors and warnings) if any error is found.
    """
    path = Path(path)
    if not path.is_file():
        raise InputValidationError([Issue.error(IssueCode.FILE_NOT_FOUND, path=str(path))])
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise InputValidationError(
            [Issue.error(IssueCode.FILE_NOT_XLSX, path=str(path), suffix=path.suffix)]
        )
    sheets = _load_sheets(path)
    issues: list[Issue] = []
    table, records = _parse_sheet(sheets.pieces, issues)
    pieces = _merge_duplicates(records, issues)
    issues.extend(_cross_row_warnings(records))
    inventory: list[InventoryStock] = []
    if sheets.inventory is not None and sheets.inventory_title is not None:
        sheet_issues: list[Issue] = []
        inventory = _parse_inventory(sheets.inventory, sheet_issues)
        issues.extend(_in_sheet(i, sheets.inventory_title) for i in sheet_issues)
        unused = list(
            dict.fromkeys(i.size for i in inventory if i.size not in {p.size for p in pieces})
        )
        if unused and pieces:
            issues.append(
                Issue.warning(
                    IssueCode.INVENTORY_SIZE_UNUSED, sizes=unused, sheet=sheets.inventory_title
                )
            )

    errors = [i for i in issues if i.is_error]
    warnings = [i for i in issues if not i.is_error]
    if errors:
        raise InputValidationError(errors + warnings)
    return ReadResult(
        path=path,
        pieces=tuple(pieces),
        warnings=tuple(warnings),
        table=table,
        inventory=tuple(inventory),
        inventory_sheet=sheets.inventory_title,
    )


def is_inventory_sheet(title: str) -> bool:
    return title.strip().casefold() in INVENTORY_SHEET_NAMES


def _load_sheets(path: Path) -> _Sheets:
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except PermissionError as exc:
        raise InputValidationError([Issue.error(IssueCode.FILE_LOCKED, path=str(path))]) from exc
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise InputValidationError([Issue.error(IssueCode.FILE_INVALID, path=str(path))]) from exc
    try:
        piece_sheets = [ws for ws in workbook.worksheets if not is_inventory_sheet(ws.title)]
        inventory_sheets = [ws for ws in workbook.worksheets if is_inventory_sheet(ws.title)]
        pieces = (
            [tuple(row) for row in piece_sheets[0].iter_rows(values_only=True)]
            if piece_sheets
            else []
        )
        if not inventory_sheets:
            return _Sheets(pieces=pieces, inventory=None, inventory_title=None)
        inventory = [tuple(row) for row in inventory_sheets[0].iter_rows(values_only=True)]
        return _Sheets(
            pieces=pieces, inventory=inventory, inventory_title=inventory_sheets[0].title
        )
    except (KeyError, ValueError, OSError, zipfile.BadZipFile) as exc:
        raise InputValidationError([Issue.error(IssueCode.FILE_INVALID, path=str(path))]) from exc
    finally:
        workbook.close()


def _in_sheet(issue: Issue, sheet: str) -> Issue:
    return Issue(issue.code, issue.severity, {**issue.params, "sheet": sheet})


def _parse_sheet(
    raw_rows: Sequence[tuple[object, ...]], issues: list[Issue]
) -> tuple[InputTable, list[_Record]]:
    empty_table = InputTable(headers=(), row_numbers=(), rows=())
    if not raw_rows or _is_blank_row(raw_rows[0]):
        issues.append(Issue.error(IssueCode.NO_DATA))
        return empty_table, []

    headers = tuple("" if v is None else str(v).strip() for v in raw_rows[0])
    columns = _locate_columns(headers, issues)
    width = len(headers)

    row_numbers: list[int] = []
    kept_rows: list[tuple[object, ...]] = []
    records: list[_Record] = []
    for row_number, raw in _numbered_rows(raw_rows):
        if _is_blank_row(raw):
            continue
        padded = tuple(raw[:width]) + (None,) * max(0, width - len(raw))
        row_numbers.append(row_number)
        kept_rows.append(padded)
        if columns is not None:
            record = _parse_row(row_number, padded, columns, issues)
            if record is not None:
                records.append(record)

    if not kept_rows:
        issues.append(Issue.error(IssueCode.NO_DATA))
    table = InputTable(headers=headers, row_numbers=tuple(row_numbers), rows=tuple(kept_rows))
    return table, records


def _numbered_rows(
    raw_rows: Sequence[tuple[object, ...]],
) -> Iterator[tuple[int, tuple[object, ...]]]:
    for offset, raw in enumerate(raw_rows[1:]):
        yield HEADER_ROW + 1 + offset, raw


def _locate_columns(
    headers: Sequence[str],
    issues: list[Issue],
    required: Sequence[Column] = PIECE_COLUMNS,
    missing_code: IssueCode = IssueCode.MISSING_COLUMNS,
) -> dict[Column, int] | None:
    found: dict[Column, int] = {}
    for index, header in enumerate(headers):
        column = COLUMN_ALIASES.get(header.casefold())
        if column is None or column not in required:
            continue
        if column in found:
            issues.append(Issue.error(IssueCode.DUPLICATE_COLUMN, column=str(column)))
            continue
        found[column] = index
    missing = [str(c) for c in required if c not in found]
    if missing:
        issues.append(Issue.error(missing_code, columns=missing))
        return None
    return found


def _parse_row(
    row: int, values: tuple[object, ...], columns: dict[Column, int], issues: list[Issue]
) -> _Record | None:
    cells = {column: values[index] for column, index in columns.items()}
    for column, value in cells.items():
        if _is_blank(value):
            issues.append(Issue.error(IssueCode.EMPTY_VALUE, row=row, column=str(column)))
    if any(_is_blank(v) for v in cells.values()):
        return None

    size = _parse_size(row, cells[Column.SIZE], issues)
    no = _parse_no(row, cells[Column.NO], issues)
    length_mm = _parse_length(row, cells[Column.LENGTH], issues)
    quantity = _parse_quantity(row, cells[Column.QUANTITY], issues)
    if size is None or no is None or length_mm is None or quantity is None:
        return None
    return _Record(row=row, size=size, no=no, length_mm=length_mm, quantity=quantity)


def _parse_inventory(
    raw_rows: Sequence[tuple[object, ...]], issues: list[Issue]
) -> list[InventoryStock]:
    """The inventory sheet; an empty sheet (or headers only) means no inventory.

    Rows of the same size and length are merged.
    """
    if not raw_rows or _is_blank_row(raw_rows[0]):
        return []
    headers = tuple("" if v is None else str(v).strip() for v in raw_rows[0])
    columns = _locate_columns(
        headers, issues, INVENTORY_COLUMNS, IssueCode.INVENTORY_MISSING_COLUMNS
    )
    if columns is None:
        return []
    merged: dict[tuple[str, int], InventoryStock] = {}
    for row_number, raw in _numbered_rows(raw_rows):
        if _is_blank_row(raw):
            continue
        values = tuple(raw) + (None,) * max(0, len(headers) - len(raw))
        cells = {column: values[index] for column, index in columns.items()}
        blanks = [column for column, value in cells.items() if _is_blank(value)]
        for column in blanks:
            issues.append(Issue.error(IssueCode.EMPTY_VALUE, row=row_number, column=str(column)))
        if blanks:
            continue
        size = _parse_size(row_number, cells[Column.SIZE], issues)
        length_mm = _parse_length(row_number, cells[Column.LENGTH], issues)
        quantity = _parse_quantity(row_number, cells[Column.QUANTITY], issues)
        if size is None or length_mm is None or quantity is None:
            continue
        key = (size, length_mm)
        previous = merged.get(key)
        merged[key] = InventoryStock(
            size=size,
            length_mm=length_mm,
            quantity=quantity + (previous.quantity if previous else 0),
            rows=(*previous.rows, row_number) if previous else (row_number,),
        )
    return list(merged.values())


# --- cell parsing ---------------------------------------------------------------


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_blank_row(values: Iterable[object]) -> bool:
    return all(_is_blank(v) for v in values)


def _as_number(value: object) -> float | None:
    """A finite number from a numeric cell or a numeric-looking text cell."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str) and _NUMBER_TEXT.match(value.strip()):
        number = float(value.strip())
    else:
        return None
    return number if math.isfinite(number) else None


def _number_text(number: float) -> str:
    """``12`` / ``12.0`` -> ``"12"``; ``12.5`` -> ``"12.5"``."""
    return str(int(number)) if number.is_integer() else repr(number)


def _parse_identifier(row: int, value: object, column: Column, issues: list[Issue]) -> str | None:
    if isinstance(value, str):
        number = _as_number(value)
        return value.strip() if number is None else _number_text(number)
    number = _as_number(value)
    if number is None:
        issues.append(
            Issue.error(IssueCode.INVALID_TEXT, row=row, column=str(column), value=str(value))
        )
        return None
    return _number_text(number)


def _parse_size(row: int, value: object, issues: list[Issue]) -> str | None:
    text = _parse_identifier(row, value, Column.SIZE, issues)
    return None if text is None else "".join(text.split()).upper()


def _parse_no(row: int, value: object, issues: list[Issue]) -> str | None:
    return _parse_identifier(row, value, Column.NO, issues)


def _parse_length(row: int, value: object, issues: list[Issue]) -> int | None:
    metres = _as_number(value)
    if metres is None:
        issues.append(Issue.error(IssueCode.LENGTH_NOT_NUMBER, row=row, value=str(value)))
        return None
    if metres <= 0:
        issues.append(Issue.error(IssueCode.LENGTH_NOT_POSITIVE, row=row, value=metres))
        return None
    exact_mm = metres * 1000
    length_mm = round(exact_mm)
    if length_mm <= 0:
        issues.append(Issue.error(IssueCode.LENGTH_ROUNDS_TO_ZERO, row=row, value=metres))
        return None
    if abs(exact_mm - length_mm) > SUB_MM_TOLERANCE:
        issues.append(
            Issue.warning(IssueCode.LENGTH_SUB_MM, row=row, value=metres, length_mm=length_mm)
        )
    return length_mm


def _parse_quantity(row: int, value: object, issues: list[Issue]) -> int | None:
    number = _as_number(value)
    if number is None or not number.is_integer() or number <= 0:
        issues.append(Issue.error(IssueCode.QUANTITY_INVALID, row=row, value=str(value)))
        return None
    return int(number)


# --- cross-row rules -------------------------------------------------------------


def _merge_duplicates(records: Sequence[_Record], issues: list[Issue]) -> list[Piece]:
    """Apply the duplicate-No rules of D6 and merge equal duplicates."""
    by_no: dict[str, list[_Record]] = {}
    for record in records:
        by_no.setdefault(record.no, []).append(record)

    pieces: list[Piece] = []
    for no, group in by_no.items():
        rows = [r.row for r in group]
        sizes = list(dict.fromkeys(r.size for r in group))
        if len(sizes) > 1:
            issues.append(Issue.error(IssueCode.DUP_NO_DIFF_SIZE, no=no, sizes=sizes, rows=rows))
            continue
        lengths = list(dict.fromkeys(r.length_mm for r in group))
        if len(lengths) > 1:
            issues.append(
                Issue.error(
                    IssueCode.DUP_NO_DIFF_LENGTH,
                    size=sizes[0],
                    no=no,
                    lengths_mm=lengths,
                    rows=rows,
                )
            )
            continue
        quantity = sum(r.quantity for r in group)
        if len(group) > 1:
            issues.append(
                Issue.warning(
                    IssueCode.DUP_NO_MERGED, size=sizes[0], no=no, quantity=quantity, rows=rows
                )
            )
        pieces.append(
            Piece(
                size=sizes[0],
                no=no,
                length_mm=lengths[0],
                quantity=quantity,
                rows=tuple(rows),
            )
        )
    pieces.sort(key=lambda p: p.rows[0])
    return pieces


def _cross_row_warnings(records: Sequence[_Record]) -> list[Issue]:
    warnings: list[Issue] = []
    if records and all(r.length_mm >= LIKELY_MM_THRESHOLD_M * 1000 for r in records):
        warnings.append(Issue.warning(IssueCode.LENGTH_MAYBE_MM))

    sizes = list(dict.fromkeys(r.size for r in records))
    for size in sizes:
        if not _NUMBER_TEXT.match(size):
            continue
        similar = [
            other
            for other in sizes
            if (m := _PREFIXED_SIZE.match(other)) is not None and m.group(2) == size
        ]
        if similar:
            warnings.append(
                Issue.warning(IssueCode.SIZE_AMBIGUOUS_PREFIX, size=size, similar=similar)
            )
    return warnings
