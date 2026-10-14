"""Phase 8: inventory / remnants from earlier jobs (optional ``Stock`` sheet)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import openpyxl
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rebar_optimizer.cli import EXIT_OK, main
from rebar_optimizer.domain.errors import InputValidationError, InvariantViolation, IssueCode
from rebar_optimizer.domain.models import (
    BarGroup,
    CuttingPlan,
    InventoryStock,
    Piece,
    SolveStatus,
    StockSource,
)
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.i18n import th
from rebar_optimizer.io.reader import read_pieces
from rebar_optimizer.io.template import write_template
from rebar_optimizer.io.writer import WriteSource, write_plan
from rebar_optimizer.services import planner
from rebar_optimizer.services.invariants import verify_plan
from rebar_optimizer.services.postprocess import assign_stock
from rebar_optimizer.solver.base import RawBar, stock_cost
from rebar_optimizer.solver.greedy import GreedySolver

PIECE_HEADERS = ("Size", "No", "Length", "Quantity")
STOCK_HEADERS = ("Size", "Length", "Quantity")
INV = StockSource.INVENTORY


def workbook(
    path: Path,
    sheets: Sequence[tuple[str, Sequence[Sequence[object]]]],
) -> Path:
    """Write ``(title, rows)`` sheets in order; rows include the header row."""
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title, rows in sheets:
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(list(row))
    book.save(path)
    return path


def settings_(*stocks: int, **changes: object) -> OptimizerSettings:
    return replace(OptimizerSettings(stock_lengths_mm=stocks, time_limit_s=5), **changes)  # type: ignore[arg-type]


def stock(size: str, length_mm: int, quantity: int) -> InventoryStock:
    return InventoryStock(size, length_mm, quantity, (2,))


def inventory_bars(plan: CuttingPlan, size_index: int = 0) -> dict[int, int]:
    used: dict[int, int] = {}
    for group in plan.sizes[size_index].groups:
        if group.bar.source is INV:
            used[group.bar.stock_length_mm] = used.get(group.bar.stock_length_mm, 0) + group.count
    return used


# --- reader ------------------------------------------------------------------------------


def test_reads_stock_sheet_and_merges_equal_lengths(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [
            ("ชิ้นงาน", [PIECE_HEADERS, ("DB12", 1, 3.5, 4)]),
            (
                "Stock",
                [STOCK_HEADERS, (" db12 ", 7.3, 2), (None, None, None), ("DB12", "7.300", 1)],
            ),
        ],
    )
    result = read_pieces(path)
    assert result.inventory_sheet == "Stock"
    assert result.inventory == (InventoryStock("DB12", 7300, 3, (2, 4)),)
    assert [p.no for p in result.pieces] == ["1"]


def test_thai_sheet_name_and_headers_and_stock_sheet_first(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [
            ("คงคลัง", [("ขนาด", "ความยาว", "จำนวน"), ("DB16", 6, 1)]),
            ("รายการ", [PIECE_HEADERS, ("DB16", "A", 2.5, 3)]),
        ],
    )
    result = read_pieces(path)
    assert result.inventory == (InventoryStock("DB16", 6000, 1, (2,)),)
    assert result.pieces == (Piece("DB16", "A", 2500, 3, (2,)),)


def test_empty_stock_sheet_means_no_inventory(tmp_path: Path) -> None:
    for rows in ([], [STOCK_HEADERS]):
        path = workbook(
            tmp_path / f"in{len(rows)}.xlsx",
            [("P", [PIECE_HEADERS, ("DB12", 1, 3, 1)]), ("stock", rows)],
        )
        result = read_pieces(path)
        assert result.inventory == ()
        assert result.warnings == ()


def test_stock_sheet_errors_name_the_sheet_and_row(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [
            ("P", [PIECE_HEADERS, ("DB12", 1, 3, 1)]),
            ("Stock", [STOCK_HEADERS, ("DB12", "x", 1), ("DB12", 6, 0), ("DB12", None, 1)]),
        ],
    )
    with pytest.raises(InputValidationError) as info:
        read_pieces(path)
    codes = [(i.code, i.params.get("row"), i.params.get("sheet")) for i in info.value.errors]
    assert codes == [
        (IssueCode.LENGTH_NOT_NUMBER, 2, "Stock"),
        (IssueCode.QUANTITY_INVALID, 3, "Stock"),
        (IssueCode.EMPTY_VALUE, 4, "Stock"),
    ]
    assert th.format_issue(info.value.errors[0]).startswith("ชีต Stock: แถว 2:")


def test_stock_sheet_missing_columns(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [("P", [PIECE_HEADERS, ("DB12", 1, 3, 1)]), ("Stock", [("Size", "Qty"), ("DB12", 1)])],
    )
    with pytest.raises(InputValidationError) as info:
        read_pieces(path)
    (error,) = info.value.errors
    assert error.code is IssueCode.INVENTORY_MISSING_COLUMNS
    assert error.params["columns"] == ["Length", "Quantity"]
    assert "ชีต Stock" in th.format_issue(error)


def test_inventory_of_unknown_size_warns(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [
            ("P", [PIECE_HEADERS, ("DB12", 1, 3, 1)]),
            ("Stock", [STOCK_HEADERS, ("DB20", 6, 1), ("DB12", 6, 1)]),
        ],
    )
    (warning,) = read_pieces(path).warnings
    assert warning.code is IssueCode.INVENTORY_SIZE_UNUSED
    assert warning.params["sizes"] == ["DB20"]


def test_template_has_empty_stock_sheet(tmp_path: Path) -> None:
    result = read_pieces(write_template(tmp_path / "t.xlsx"))
    assert result.inventory_sheet == "Stock"
    assert result.inventory == ()
    assert result.pieces


# --- planning ----------------------------------------------------------------------------


def test_inventory_bar_replaces_a_purchase() -> None:
    result = planner.plan(
        [Piece("DB12", "1", 6000, 1, (2,))], settings_(12000), inventory=[stock("DB12", 6000, 1)]
    )
    size = result.sizes[0]
    assert (size.new_stock_mm, size.inventory_stock_mm) == (0, 6000)
    assert size.status is SolveStatus.OPTIMAL
    assert size.lower_bound_mm == 0


def test_inventory_unused_when_it_saves_no_purchase() -> None:
    # 4 x 5 m need two 10 m bars either way; one 5 m inventory bar cannot save a bar.
    result = planner.plan(
        [Piece("DB12", "1", 5000, 4, (2,))], settings_(10000), inventory=[stock("DB12", 5000, 1)]
    )
    size = result.sizes[0]
    assert (size.new_stock_mm, size.inventory_stock_mm) == (20000, 0)
    assert size.lower_bound_mm == 20000  # greedy meets the bound: proven, ILP skipped (D8)


def test_availability_is_respected() -> None:
    # Three 6 m pieces, only two 6 m bars on hand -> one 7 m bar must be bought.
    result = planner.plan(
        [Piece("DB12", "1", 6000, 3, (2,))], settings_(7000), inventory=[stock("DB12", 6000, 2)]
    )
    size = result.sizes[0]
    assert inventory_bars(result) == {6000: 2}
    assert size.new_stock_mm == 7000


def test_shortest_suitable_inventory_bar_is_used() -> None:
    result = planner.plan(
        [Piece("DB12", "1", 4000, 1, (2,))],
        settings_(12000),
        inventory=[stock("DB12", 9000, 1), stock("DB12", 5000, 1)],
    )
    assert inventory_bars(result) == {5000: 1}
    assert result.sizes[0].new_stock_mm == 0


def test_single_length_mode_applies_to_purchase_only() -> None:
    pieces = [Piece("DB12", "1", 7000, 1, (2,)), Piece("DB12", "2", 5000, 2, (3,))]
    result = planner.plan(
        pieces,
        settings_(10000, 12000, stock_mode=StockMode.SINGLE),
        inventory=[stock("DB12", 7000, 1)],
    )
    size = result.sizes[0]
    assert inventory_bars(result) == {7000: 1}
    assert size.single_stock_mm == 10000
    assert size.new_stock_mm == 10000


def test_single_length_mode_all_from_inventory() -> None:
    result = planner.plan(
        [Piece("DB12", "1", 3000, 2, (2,))],
        settings_(10000, 12000, stock_mode=StockMode.SINGLE),
        inventory=[stock("DB12", 6000, 1)],
    )
    size = result.sizes[0]
    assert (size.new_stock_mm, size.single_stock_mm) == (0, None)


def test_long_inventory_bar_does_not_rescue_oversize_pieces() -> None:
    # Unplaced is decided against the longest *new* stock: inventory is limited.
    result = planner.plan(
        [Piece("DB12", "1", 13000, 1, (2,)), Piece("DB12", "2", 2000, 1, (3,))],
        settings_(12000),
        inventory=[stock("DB12", 14000, 1)],
    )
    assert [(u.no, u.quantity) for u in result.unplaced] == [("1", 1)]


def test_inventory_of_other_sizes_is_ignored() -> None:
    result = planner.plan(
        [Piece("DB12", "1", 6000, 1, (2,))], settings_(12000), inventory=[stock("DB16", 6000, 1)]
    )
    assert result.sizes[0].new_stock_mm == 12000


def test_column_generation_with_inventory() -> None:
    pieces = [
        Piece("DB12", str(i), length, 3 + i % 4, (i + 2,))
        for i, length in enumerate((900, 1150, 1730, 2210, 2480, 3170, 3330, 4120, 4760))
    ]
    inventory = [stock("DB12", 4800, 3), stock("DB12", 2500, 4)]
    result = planner.plan(pieces, settings_(10000, 12000, max_patterns=5), inventory=inventory)
    size = result.sizes[0]
    assert size.status in {SolveStatus.HEURISTIC, SolveStatus.GREEDY}
    used = inventory_bars(result)
    assert used.get(4800, 0) <= 3 and used.get(2500, 0) <= 4
    plain = planner.plan(pieces, settings_(10000, 12000, max_patterns=5))
    assert size.new_stock_mm <= plain.sizes[0].new_stock_mm


def test_greedy_fills_inventory_first() -> None:
    result = GreedySolver().solve({6000: 2, 3000: 1}, (12000,), 0, inventory={6000: 1})
    assert stock_cost(result.bars) == (12000, 6000)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    lengths=st.lists(st.integers(500, 6000), min_size=1, max_size=4),
    counts=st.lists(st.integers(1, 3), min_size=4, max_size=4),
    inventory=st.dictionaries(st.integers(1000, 9000), st.integers(1, 2), max_size=3),
    single=st.booleans(),
    kerf_mm=st.sampled_from([0, 3]),
)
def test_property_inventory_never_increases_purchase(
    lengths: list[int],
    counts: list[int],
    inventory: dict[int, int],
    single: bool,
    kerf_mm: int,
) -> None:
    pieces = [
        Piece("DB12", str(i), length, counts[i], (i + 2,)) for i, length in enumerate(lengths)
    ]
    mode = StockMode.SINGLE if single else StockMode.MIXED
    config = settings_(6000, 10000, stock_mode=mode, kerf_mm=kerf_mm, num_workers=2)
    items = [stock("DB12", length, n) for length, n in inventory.items()]
    with_inventory = planner.plan(pieces, config, inventory=items)  # verify_plan runs inside
    without = planner.plan(pieces, config)
    a, b = with_inventory.sizes[0], without.sizes[0]
    if a.status is SolveStatus.OPTIMAL and b.status is SolveStatus.OPTIMAL:
        assert a.new_stock_mm <= b.new_stock_mm


# --- post-processing ---------------------------------------------------------------------


def test_assign_stock_moves_a_purchase_onto_spare_inventory() -> None:
    bars = [RawBar.of(10000, [4000]), RawBar.of(10000, [8000])]
    result = assign_stock(bars, (10000,), 0, {5000: 1, 6000: 1})
    assert sorted((b.stock_length_mm, b.source, b.lengths) for b in result) == [
        (5000, INV, (4000,)),
        (10000, StockSource.NEW, (8000,)),
    ]


def test_assign_stock_without_inventory_only_downsizes() -> None:
    result = assign_stock([RawBar.of(12000, [4000])], (6000, 12000), 0)
    assert result == [RawBar.of(6000, [4000])]


# --- invariant mutations (D7 rule 3 with inventory) ------------------------------------------


@pytest.fixture(scope="module")
def stocked() -> tuple[list[Piece], list[InventoryStock], CuttingPlan]:
    pieces = [Piece("DB12", "1", 6000, 2, (2,))]
    inventory = [stock("DB12", 6000, 2)]
    return pieces, inventory, planner.plan(pieces, settings_(12000), inventory=inventory)


def test_mutation_inventory_overused(
    stocked: tuple[list[Piece], list[InventoryStock], CuttingPlan],
) -> None:
    pieces, inventory, plan = stocked
    verify_plan(pieces, plan, plan.settings, inventory)
    with pytest.raises(InvariantViolation, match="available"):
        verify_plan(pieces, plan, plan.settings, [stock("DB12", 6000, 1)])


def test_mutation_inventory_bar_not_in_input(
    stocked: tuple[list[Piece], list[InventoryStock], CuttingPlan],
) -> None:
    pieces, inventory, plan = stocked
    with pytest.raises(InvariantViolation):
        verify_plan(pieces, plan, plan.settings)
    group = plan.sizes[0].groups[0]
    longer = BarGroup(replace(group.bar, stock_length_mm=7000), group.count)
    mutated = replace(plan, sizes=(replace(plan.sizes[0], groups=(longer,)),))
    with pytest.raises(InvariantViolation):
        verify_plan(pieces, mutated, plan.settings, inventory)


def test_mutation_bound_above_purchase(
    stocked: tuple[list[Piece], list[InventoryStock], CuttingPlan],
) -> None:
    pieces, inventory, plan = stocked
    mutated = replace(plan, sizes=(replace(plan.sizes[0], lower_bound_mm=1),))
    with pytest.raises(InvariantViolation, match="purchase"):
        verify_plan(pieces, mutated, plan.settings, inventory)


# --- output ------------------------------------------------------------------------------


def rows(sheet: openpyxl.worksheet.worksheet.Worksheet) -> list[tuple[object, ...]]:
    return [tuple(r) for r in sheet.iter_rows(min_row=2, values_only=True)]


def test_writer_separates_purchase_and_inventory(tmp_path: Path) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [
            ("P", [PIECE_HEADERS, ("DB12", 1, 6, 3)]),
            ("Stock", [STOCK_HEADERS, ("DB12", 6, 2), ("DB12", 2, 5)]),
        ],
    )
    source = read_pieces(path)
    plan = planner.plan(source.pieces, settings_(7000), inventory=source.inventory)
    out = write_plan(
        tmp_path / "out.xlsx",
        plan,
        WriteSource(
            source.pieces,
            source.table,
            source.path,
            source.warnings,
            inventory=source.inventory,
            inventory_sheet=source.inventory_sheet,
        ),
    )
    book = openpyxl.load_workbook(out)
    assert rows(book[th.SHEET_NAMES["inventory"]]) == [
        ("DB12", 6.0, 2, 2, 0, 12.0, pytest.approx(12 * 144 / 162)),
        ("DB12", 2.0, 5, 0, 5, 0.0, 0.0),
        (th.TOTAL_TEXT, None, 7, 2, 5, 12.0, pytest.approx(12 * 144 / 162)),
    ]
    purchase = rows(book[th.SHEET_NAMES["purchase"]])
    assert purchase[0][:4] == ("DB12", 7.0, 1, 7.0)
    assert purchase[-1][:4] == (th.TOTAL_TEXT, None, 1, 7.0)
    sources = {r[4] for r in rows(book[th.SHEET_NAMES["plan"]])}
    assert sources == {"ซื้อใหม่", "คงคลัง"}
    summary = rows(book[th.SHEET_NAMES["summary"]])[0]
    assert summary[1] == "7.000 × 1, 6.000 × 2 (คงคลัง)"
    assert summary[3:6] == (19.0, 7.0, 12.0)
    assert summary[11] == pytest.approx(7 * 144 / 162)  # purchase weight: new stock only
    status = {r[0]: r[1] for r in rows(book[th.SHEET_NAMES["status"]])}
    assert status["เหล็กคงคลัง"] == "ชีต Stock: 2 รายการ"


def test_cli_reports_inventory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = workbook(
        tmp_path / "in.xlsx",
        [("P", [PIECE_HEADERS, ("DB12", 1, 6, 3)]), ("Stock", [STOCK_HEADERS, ("DB12", 6, 2)])],
    )
    assert main([str(path), "--stock", "7", "-o", str(tmp_path / "o.xlsx")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "ใช้จากคงคลัง: 6.000 ม. × 2 เส้น" in out
    assert "สั่งซื้อ: 7.000 ม. × 1 เส้น" in out
    assert "ซื้อใหม่ 7.000 ม. | ใช้จากคงคลัง 12.000 ม." in out
