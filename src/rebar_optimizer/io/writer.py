"""Write a ``CuttingPlan`` to an .xlsx workbook (6.2).

Every figure is derived from the raw plan via ``services.summary`` and
``solver.kerf``; the "check" sheet recounts pieces from the bars against the
input, never copying from the input. The file is written to a temporary file in
the target folder and then renamed, so a failed save never leaves a half file.
"""

from __future__ import annotations

import contextlib
import copy
import os
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from rebar_optimizer import __version__
from rebar_optimizer.domain.errors import Issue, IssueCode, OutputWriteError
from rebar_optimizer.domain.models import (
    CuttingPlan,
    InventoryStock,
    Piece,
    PieceKey,
    SolveStatus,
    StockSource,
)
from rebar_optimizer.domain.weights import unit_weight_kg_per_m
from rebar_optimizer.i18n import th
from rebar_optimizer.io.reader import InputTable
from rebar_optimizer.services.summary import (
    SizeSummary,
    bar_counts,
    cut_runs,
    group_details,
    summarize_size,
)

METRE_FORMAT = "0.000"
KG_FORMAT = "0.0"
PERCENT_FORMAT = "0.0%"
MAX_COLUMN_WIDTH = 60
HEADER_EXTRA_WIDTH = 3
"""Room for the autofilter button next to a header."""
_BOLD = Font(bold=True)
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_TOTAL_FILL = PatternFill("solid", fgColor="DDEBF7")
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_CENTER = Alignment(horizontal="center", vertical="center")
_WRAP = Alignment(vertical="top", wrap_text=True)
_FAIL_FONT = Font(bold=True, color="C00000")
_TAB_COLORS = {"bar_counts": "2E75B6", "purchase": "2E75B6", "unplaced": "C00000"}


@dataclass(frozen=True)
class WriteSource:
    """What the writer needs besides the plan."""

    pieces: Sequence[Piece]
    table: InputTable
    input_path: Path | None = None
    warnings: Sequence[Issue] = ()
    """Reader warnings, shown on the status sheet."""
    inventory: Sequence[InventoryStock] = ()
    """Bars on hand from the input's inventory sheet (phase 8)."""
    inventory_sheet: str | None = None


Row = Sequence[object]


def write_plan(
    path: str | Path,
    plan: CuttingPlan,
    source: WriteSource,
    created_at: datetime | None = None,
) -> Path:
    """Write the output workbook. Raises ``OutputWriteError`` if it cannot be saved."""
    path = Path(path)
    workbook = build_workbook(plan, source, created_at or datetime.now())
    _save_atomically(workbook, path)
    return path


def build_workbook(
    plan: CuttingPlan, source: WriteSource, created_at: datetime
) -> openpyxl.Workbook:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    writer_issues: list[Issue] = []
    weights = _unit_weights(plan, writer_issues)
    summaries = [summarize_size(sp) for sp in plan.sizes]

    _plan_sheet(workbook, plan)
    _details_sheet(workbook, plan)
    _summary_sheet(workbook, plan, summaries, weights)
    _bar_counts_sheet(workbook, plan)
    _purchase_sheet(workbook, summaries, weights)
    _inventory_sheet(workbook, plan, source.inventory, weights)
    _remnants_sheet(workbook, plan)
    _unplaced_sheet(workbook, plan)
    _check_sheet(workbook, plan, source.pieces)
    issues = [*source.warnings, *plan.issues, *writer_issues]
    _status_sheet(workbook, plan, source, created_at, issues)
    _input_sheet(workbook, source.table)
    return workbook


# --- sheets ---------------------------------------------------------------------------


def _plan_sheet(workbook: openpyxl.Workbook, plan: CuttingPlan) -> None:
    rows: list[Row] = []
    for size_plan in plan.sizes:
        details = group_details(size_plan)
        for index, (group, detail) in enumerate(zip(size_plan.groups, details, strict=True), 1):
            usage = detail.usage
            rows.append(
                (
                    size_plan.size,
                    index,
                    group.count,
                    _m(usage.stock_length_mm),
                    th.STOCK_SOURCE_LABELS[group.bar.source],
                    th.format_cuts(cut_runs(group.bar.cuts)),
                    usage.piece_count,
                    _m(usage.pieces_mm),
                    _m(usage.kerf_loss_mm),
                    _m(usage.remnant_mm),
                    th.CHECK_OK if detail.is_reusable else None,
                    _m(detail.longest_cuttable_mm),
                )
            )
    _table_sheet(
        workbook,
        "plan",
        rows,
        {3: METRE_FORMAT, 7: METRE_FORMAT, 8: METRE_FORMAT, 9: METRE_FORMAT, 11: METRE_FORMAT},
    )


def _details_sheet(workbook: openpyxl.Workbook, plan: CuttingPlan) -> None:
    rows: list[Row] = []
    for size_plan in plan.sizes:
        for index, group in enumerate(size_plan.groups, start=1):
            per_bar = Counter((c.no, c.length_mm) for c in group.bar.cuts)
            for (no, length), count in per_bar.items():
                rows.append(
                    (
                        size_plan.size,
                        index,
                        _m(group.bar.stock_length_mm),
                        th.STOCK_SOURCE_LABELS[group.bar.source],
                        no,
                        _m(length),
                        count,
                        group.count,
                        count * group.count,
                    )
                )
    _table_sheet(workbook, "details", rows, {2: METRE_FORMAT, 5: METRE_FORMAT})


def _summary_sheet(
    workbook: openpyxl.Workbook,
    plan: CuttingPlan,
    summaries: Sequence[SizeSummary],
    weights: dict[str, float | None],
) -> None:
    rows: list[Row] = []
    for size_plan, summary in zip(plan.sizes, summaries, strict=True):
        w = weights[size_plan.size]
        rows.append(
            (
                size_plan.size,
                _stock_breakdown(summary.bars_by_stock, summary.inventory_by_stock),
                summary.bar_count,
                _m(summary.stock_mm),
                _m(summary.new_stock_mm),
                _m(summary.inventory_mm),
                _m(summary.pieces_mm),
                _m(summary.reusable_mm),
                _m(summary.waste_mm),
                _m(summary.kerf_mm),
                summary.utilization if summary.stock_mm else None,
                _kg(summary.new_stock_mm, w),
                _kg(summary.reusable_mm, w),
                _kg(summary.waste_mm, w),
                th.STATUS_LABELS[size_plan.status] if size_plan.groups else None,
                _m(size_plan.lower_bound_mm),
                size_plan.gap if size_plan.groups else None,
            )
        )
    if rows:
        total_stock = sum(s.stock_mm for s in summaries)
        total_pieces = sum(s.pieces_mm for s in summaries)
        rows.append(
            (
                th.TOTAL_TEXT,
                None,
                sum(s.bar_count for s in summaries),
                _m(total_stock),
                _m(sum(s.new_stock_mm for s in summaries)),
                _m(sum(s.inventory_mm for s in summaries)),
                _m(total_pieces),
                _m(sum(s.reusable_mm for s in summaries)),
                _m(sum(s.waste_mm for s in summaries)),
                _m(sum(s.kerf_mm for s in summaries)),
                total_pieces / total_stock if total_stock else None,
                _sum_known(_kg(s.new_stock_mm, weights[s.size]) for s in summaries),
                _sum_known(_kg(s.reusable_mm, weights[s.size]) for s in summaries),
                _sum_known(_kg(s.waste_mm, weights[s.size]) for s in summaries),
                None,
                None,
                None,
            )
        )
    formats = {i: METRE_FORMAT for i in (3, 4, 5, 6, 7, 8, 9, 15)}
    formats.update(
        {10: PERCENT_FORMAT, 16: PERCENT_FORMAT, 11: KG_FORMAT, 12: KG_FORMAT, 13: KG_FORMAT}
    )
    sheet = _table_sheet(workbook, "summary", rows, formats)
    if rows:
        _bold_row(sheet, len(rows) + 1)


def _bar_counts_sheet(workbook: openpyxl.Workbook, plan: CuttingPlan) -> None:
    """One sentence per size and stock length, the same lines as the CLI and GUI."""
    rows: list[Row] = [
        (th.format_bar_count(c.size, c.count, c.stock_length_mm, c.inventory),)
        for c in bar_counts(plan)
    ]
    sheet = _table_sheet(workbook, "bar_counts", rows, {}, autofilter=False)
    for row_cells in sheet.iter_rows(min_row=2):
        for cell in row_cells:
            cell.font = Font(size=12)
    _fit_columns(sheet, data_scale=12 / 11)


def _purchase_sheet(
    workbook: openpyxl.Workbook,
    summaries: Sequence[SizeSummary],
    weights: dict[str, float | None],
) -> None:
    rows: list[Row] = []
    subtotal_rows: list[int] = []
    grand_bars = 0
    grand_mm = 0
    grand_kg: list[float | None] = []
    for summary in summaries:
        if not summary.bars_by_stock:
            continue
        w = weights[summary.size]
        for stock, count in summary.bars_by_stock.items():
            rows.append((summary.size, _m(stock), count, _m(stock * count), _kg(stock * count, w)))
        size_bars = sum(summary.bars_by_stock.values())
        size_kg = _kg(summary.new_stock_mm, w)
        rows.append(
            (
                th.SUBTOTAL_TEXT.format(size=summary.size),
                None,
                size_bars,
                _m(summary.new_stock_mm),
                size_kg,
            )
        )
        subtotal_rows.append(len(rows) + 1)
        grand_bars += size_bars
        grand_mm += summary.new_stock_mm
        grand_kg.append(size_kg)
    if rows:
        rows.append((th.TOTAL_TEXT, None, grand_bars, _m(grand_mm), _sum_known(grand_kg)))
        subtotal_rows.append(len(rows) + 1)
    sheet = _table_sheet(
        workbook, "purchase", rows, {1: METRE_FORMAT, 3: METRE_FORMAT, 4: KG_FORMAT}
    )
    for row in subtotal_rows:
        _bold_row(sheet, row)


def _inventory_sheet(
    workbook: openpyxl.Workbook,
    plan: CuttingPlan,
    inventory: Sequence[InventoryStock],
    weights: dict[str, float | None],
) -> None:
    """Every inventory entry of the input with how many bars the plan takes
    (recounted from the bars), kept apart from the purchase list."""
    used: Counter[tuple[str, int]] = Counter()
    for size_plan in plan.sizes:
        for group in size_plan.groups:
            if group.bar.source is StockSource.INVENTORY:
                used[(size_plan.size, group.bar.stock_length_mm)] += group.count
    rows: list[Row] = []
    totals = [0, 0, 0, 0]
    used_kg: list[float | None] = []
    for item in inventory:
        n = used[(item.size, item.length_mm)]
        kg = _kg(item.length_mm * n, weights.get(item.size))
        rows.append(
            (
                item.size,
                _m(item.length_mm),
                item.quantity,
                n,
                item.quantity - n,
                _m(item.length_mm * n),
                kg,
            )
        )
        for i, value in enumerate((item.quantity, n, item.quantity - n, item.length_mm * n)):
            totals[i] += value
        used_kg.append(kg)
    if rows:
        rows.append(
            (
                th.TOTAL_TEXT,
                None,
                totals[0],
                totals[1],
                totals[2],
                _m(totals[3]),
                _sum_known(used_kg),
            )
        )
    sheet = _table_sheet(
        workbook, "inventory", rows, {1: METRE_FORMAT, 5: METRE_FORMAT, 6: KG_FORMAT}
    )
    if rows:
        _bold_row(sheet, len(rows) + 1)


def _remnants_sheet(workbook: openpyxl.Workbook, plan: CuttingPlan) -> None:
    rows: list[Row] = [
        (sp.size, _m(r.length_mm), r.count, _m(r.longest_cuttable_mm))
        for sp in plan.sizes
        for r in sp.remnants
    ]
    _table_sheet(workbook, "remnants", rows, {1: METRE_FORMAT, 3: METRE_FORMAT})


def _unplaced_sheet(workbook: openpyxl.Workbook, plan: CuttingPlan) -> None:
    rows: list[Row] = [
        (
            u.size,
            u.no,
            _m(u.length_mm),
            u.quantity,
            th.format_issue(
                Issue.warning(IssueCode.UNPLACED_TOO_LONG, longest_stock_mm=u.longest_stock_mm)
            ),
        )
        for u in plan.unplaced
    ]
    _table_sheet(workbook, "unplaced", rows, {2: METRE_FORMAT})


def _check_sheet(workbook: openpyxl.Workbook, plan: CuttingPlan, pieces: Sequence[Piece]) -> None:
    """Recount from the bars (B8): never copy the cut quantity from the input."""
    cut: Counter[PieceKey] = Counter()
    unplaced: Counter[PieceKey] = Counter()
    for size_plan in plan.sizes:
        for group in size_plan.groups:
            for c in group.bar.cuts:
                cut[(size_plan.size, c.no)] += group.count
        for u in size_plan.unplaced:
            unplaced[(u.size, u.no)] += u.quantity
    rows: list[Row] = []
    for p in pieces:
        ok = cut[p.key] + unplaced[p.key] == p.quantity
        rows.append(
            (
                p.size,
                p.no,
                p.quantity,
                cut[p.key],
                unplaced[p.key],
                th.CHECK_OK if ok else th.CHECK_FAIL,
            )
        )
    _table_sheet(workbook, "check", rows, {})


def _status_sheet(
    workbook: openpyxl.Workbook,
    plan: CuttingPlan,
    source: WriteSource,
    created_at: datetime,
    issues: Sequence[Issue],
) -> None:
    labels = th.STATUS_LABELS_SHEET
    settings = plan.settings
    worst = plan.worst_status
    rows: list[Row] = [
        (labels["input_file"], str(source.input_path.resolve()) if source.input_path else None),
        (labels["created_at"], created_at.strftime("%Y-%m-%d %H:%M:%S")),
        (labels["version"], __version__),
        (
            labels["stock_lengths"],
            ", ".join(th.format_metres(s) for s in sorted(settings.stock_lengths_mm)),
        ),
        (labels["stock_mode"], th.STOCK_MODE_LABELS[settings.stock_mode]),
        (labels["kerf"], settings.kerf_mm),
        (labels["time_limit"], settings.time_limit_s),
        (labels["seed"], settings.random_seed),
        (
            labels["inventory"],
            th.INVENTORY_SHEET_TEXT.format(
                sheet=source.inventory_sheet, count=len(source.inventory)
            )
            if source.inventory and source.inventory_sheet is not None
            else th.NONE_TEXT,
        ),
        (labels["worst_status"], th.STATUS_LABELS[worst] if worst else None),
    ]
    if any(sp.status is not SolveStatus.GREEDY for sp in plan.sizes if sp.groups):
        rows.append((labels["note"], th.REPRODUCIBILITY_NOTE))
    messages = th.format_issues(issues)
    rows.append((labels["warnings"], messages[0] if messages else th.NONE_TEXT))
    rows.extend((None, message) for message in messages[1:])
    sheet = _table_sheet(workbook, "status", rows, {}, autofilter=False)
    sheet.column_dimensions["B"].width = MAX_COLUMN_WIDTH * 1.5
    for row_cells in sheet.iter_rows(min_row=2):
        row_cells[0].font = _BOLD
        row_cells[0].alignment = Alignment(vertical="top")
        row_cells[1].alignment = _WRAP


def _input_sheet(workbook: openpyxl.Workbook, table: InputTable) -> None:
    sheet = workbook.create_sheet(th.SHEET_NAMES["input"])
    _append(sheet, (th.INPUT_ROW_HEADER, *table.headers), 1)
    for offset, (row_number, values) in enumerate(
        zip(table.row_numbers, table.rows, strict=True), start=2
    ):
        _append(sheet, (row_number, *values), offset)
    _finish(sheet, autofilter=True)


# --- helpers --------------------------------------------------------------------------


def _table_sheet(
    workbook: openpyxl.Workbook,
    key: str,
    rows: Sequence[Row],
    formats: dict[int, str],
    autofilter: bool = True,
) -> Worksheet:
    sheet = workbook.create_sheet(th.SHEET_NAMES[key])
    _append(sheet, th.HEADERS[key], 1)
    if not rows:
        sheet.append((th.NONE_TEXT,))
    for row_index, row in enumerate(rows, start=2):
        _append(sheet, row, row_index)
        for column, number_format in formats.items():
            sheet.cell(row=row_index, column=column + 1).number_format = number_format
    _finish(sheet, autofilter=autofilter and bool(rows))
    return sheet


def _append(sheet: Worksheet, values: Iterable[object], row: int) -> None:
    """Append ``values`` as Excel row ``row`` (the caller counts rows).

    Never use ``sheet.max_row`` per row: openpyxl computes it by scanning every
    cell, which made a 100,000-row input take over ten minutes to save.
    """
    cells = [_cell_value(v) for v in values]
    sheet.append(cells)
    # Text that starts with "=" must stay text, not become a formula.
    for column, value in enumerate(cells, start=1):
        if isinstance(value, str) and value.startswith("="):
            sheet.cell(row=row, column=column).data_type = "s"


def _cell_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | datetime):
        return value
    return str(value)


def _finish(sheet: Worksheet, autofilter: bool) -> None:
    """Header band, grid, centred ✓/✗, column widths, frozen header and print setup."""
    for cell in sheet[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN
        cell.border = _BORDER
    sheet.row_dimensions[1].height = 24
    _style_data_cells(sheet)
    sheet.freeze_panes = "A2"
    if autofilter and sheet.max_row > 1:
        sheet.auto_filter.ref = sheet.dimensions
    _fit_columns(sheet)
    color = next((c for key, c in _TAB_COLORS.items() if th.SHEET_NAMES[key] == sheet.title), None)
    if color:
        sheet.sheet_properties.tabColor = color
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.print_title_rows = "1:1"


def _style_data_cells(sheet: Worksheet) -> None:
    """Grid on every data cell; ✓/✗ centred and ✗ red.

    Assigning a style object makes openpyxl hash it for every cell, which took
    about a minute per sheet for ~90,000 rows. Each distinct (existing style,
    kind) is styled once and its style ids are copied to the other cells.
    """
    done: dict[tuple[tuple[int, ...], int], object] = {}
    for row_cells in sheet.iter_rows(min_row=2):
        for cell in row_cells:
            value = cell.value
            kind = 2 if value == th.CHECK_FAIL else 1 if value == th.CHECK_OK else 0
            style = cell._style  # None until the cell is first styled
            key = (tuple(style) if style is not None else (), kind)
            styled = done.get(key)
            if styled is not None:
                cell._style = copy.copy(styled)
                continue
            cell.border = _BORDER
            if kind:
                cell.alignment = _CENTER
                if kind == 2:
                    cell.font = _FAIL_FONT
            done[key] = copy.copy(cell._style)


def _fit_columns(sheet: Worksheet, data_scale: float = 1.0) -> None:
    """Widths from the longest value, header included. Thai has no spaces between
    words, so Excel cannot wrap a Thai header: the column must fit all of it."""
    widths: dict[int, float] = {}
    measured: dict[object, float] = {}  # many cells repeat the same value
    for row_index, values in enumerate(sheet.iter_rows(values_only=True), start=1):
        for column, value in enumerate(values, start=1):
            if value is None:
                continue
            width = measured.get(value)
            if width is None:
                text = f"{value:.3f}" if isinstance(value, float) else str(value)
                width = measured[value] = _display_width(text)
            if row_index == 1:
                width = width * 1.15 + HEADER_EXTRA_WIDTH  # bold text plus filter button
            else:
                width *= data_scale
            if width > widths.get(column, 0.0):
                widths[column] = width
    for column, width in widths.items():
        letter = get_column_letter(column)
        sheet.column_dimensions[letter].width = min(round(width + 3, 1), MAX_COLUMN_WIDTH)


def _display_width(text: str) -> float:
    """Approximate width in Excel character units. Thai vowel and tone marks
    above or below a consonant take no width of their own."""
    return float(sum(0 if unicodedata.combining(ch) else 1 for ch in text))


def _bold_row(sheet: Worksheet, row: int) -> None:
    """Subtotal / total row."""
    for cell in sheet[row]:
        cell.font = _BOLD
        cell.fill = _TOTAL_FILL


def _m(length_mm: int | None) -> float | None:
    """mm -> m, only at the output boundary (D1)."""
    return None if length_mm is None else length_mm / 1000


def _kg(length_mm: int, kg_per_m: float | None) -> float | None:
    return None if kg_per_m is None else length_mm / 1000 * kg_per_m


def _sum_known(values: Iterable[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _stock_breakdown(
    bars_by_stock: dict[int, int], inventory_by_stock: dict[int, int]
) -> str | None:
    parts = [f"{th.format_metres(s)} × {n}" for s, n in bars_by_stock.items()]
    parts += [
        f"{th.format_metres(s)} × {n}{th.INVENTORY_TAG}" for s, n in inventory_by_stock.items()
    ]
    return ", ".join(parts) or None


def _unit_weights(plan: CuttingPlan, issues: list[Issue]) -> dict[str, float | None]:
    weights: dict[str, float | None] = {}
    for size_plan in plan.sizes:
        weight = unit_weight_kg_per_m(size_plan.size)
        if weight is None:
            issues.append(Issue.warning(IssueCode.WEIGHT_UNKNOWN, size=size_plan.size))
        weights[size_plan.size] = weight
    return weights


def _save_atomically(workbook: openpyxl.Workbook, path: Path) -> None:
    directory = path.parent if str(path.parent) else Path()
    try:
        handle, temp_name = tempfile.mkstemp(prefix="~rebar_", suffix=".xlsx", dir=directory)
    except OSError as exc:
        raise OutputWriteError(
            [Issue.error(IssueCode.OUTPUT_FAILED, path=str(path), reason=str(exc))]
        ) from exc
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        workbook.save(temp_path)
        os.replace(temp_path, path)
    except PermissionError as exc:
        raise OutputWriteError(
            [Issue.error(IssueCode.OUTPUT_LOCKED, path=str(path), name=path.name)]
        ) from exc
    except OSError as exc:
        raise OutputWriteError(
            [Issue.error(IssueCode.OUTPUT_FAILED, path=str(path), reason=str(exc))]
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
