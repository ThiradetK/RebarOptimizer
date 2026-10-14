from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest
from openpyxl.worksheet.worksheet import Worksheet

from rebar_optimizer.cli import EXIT_UNPLACED, main
from rebar_optimizer.domain.errors import IssueCode, OutputWriteError
from rebar_optimizer.domain.models import CuttingPlan, Piece
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.i18n import th
from rebar_optimizer.io.reader import InputTable, read_pieces
from rebar_optimizer.io.template import write_template
from rebar_optimizer.io.writer import WriteSource, write_plan
from rebar_optimizer.services import planner
from tests.conftest import MakeXlsx

FIXED_TIME = datetime(2026, 10, 14, 9, 30)


def rows(sheet: Worksheet) -> list[tuple[object, ...]]:
    return [tuple(r) for r in sheet.iter_rows(min_row=2, values_only=True)]


def column(sheet: Worksheet, header: str) -> list[object]:
    headers = [c.value for c in sheet[1]]
    index = headers.index(header)
    return [r[index] for r in rows(sheet)]


def plan_and_write(
    tmp_path: Path, pieces: list[Piece], settings: OptimizerSettings, name: str = "out.xlsx"
) -> tuple[CuttingPlan, openpyxl.Workbook]:
    plan = planner.plan(pieces, settings)
    table = InputTable(
        headers=("Size", "No", "Length", "Quantity"),
        row_numbers=tuple(p.rows[0] for p in pieces),
        rows=tuple((p.size, p.no, p.length_mm / 1000, p.quantity) for p in pieces),
    )
    path = write_plan(tmp_path / name, plan, WriteSource(pieces, table), FIXED_TIME)
    return plan, openpyxl.load_workbook(path)


@pytest.fixture
def template_output(tmp_path: Path) -> tuple[CuttingPlan, openpyxl.Workbook]:
    source = read_pieces(write_template(tmp_path / "t.xlsx"))
    settings = OptimizerSettings(stock_lengths_mm=(10000, 12000), kerf_mm=3)
    plan = planner.plan(source.pieces, settings)
    ws = WriteSource(source.pieces, source.table, source.path, source.warnings)
    path = write_plan(tmp_path / "out.xlsx", plan, ws, FIXED_TIME)
    return plan, openpyxl.load_workbook(path)


# --- structure -----------------------------------------------------------------------


def test_sheet_names_and_headers(template_output: tuple[CuttingPlan, openpyxl.Workbook]) -> None:
    _, workbook = template_output
    assert workbook.sheetnames == list(th.SHEET_NAMES.values())
    assert all(len(name) <= 31 for name in workbook.sheetnames)
    for key, headers in th.HEADERS.items():
        sheet = workbook[th.SHEET_NAMES[key]]
        assert tuple(c.value for c in sheet[1]) == headers
        assert sheet.freeze_panes == "A2"
        assert all(c.font.bold for c in sheet[1])


def test_number_formats(template_output: tuple[CuttingPlan, openpyxl.Workbook]) -> None:
    _, workbook = template_output
    plan_sheet = workbook[th.SHEET_NAMES["plan"]]
    assert plan_sheet["D2"].number_format == "0.000"
    assert plan_sheet.page_setup.orientation == "landscape"
    summary = workbook[th.SHEET_NAMES["summary"]]
    assert summary["J2"].number_format == "0.0"
    assert summary["I2"].number_format == "0.0%"


# --- totals: summary sheet == sum of the per-bar sheet (D7 rule 5) ------------------------


def test_summary_matches_bar_rows(template_output: tuple[CuttingPlan, openpyxl.Workbook]) -> None:
    plan, workbook = template_output
    plan_rows = rows(workbook[th.SHEET_NAMES["plan"]])
    summary = {r[0]: r for r in rows(workbook[th.SHEET_NAMES["summary"]])}
    for size_plan in plan.sizes:
        bars = [r for r in plan_rows if r[0] == size_plan.size]
        count = sum(int(str(r[2])) for r in bars)
        stock = sum(float(str(r[2])) * float(str(r[3])) for r in bars)
        pieces = sum(float(str(r[2])) * float(str(r[6])) for r in bars)
        kerf_loss = sum(float(str(r[2])) * float(str(r[7])) for r in bars)
        remnant = sum(float(str(r[2])) * float(str(r[8])) for r in bars)
        reusable = sum(float(str(r[2])) * float(str(r[8])) for r in bars if r[9] == th.CHECK_OK)
        row = summary[size_plan.size]
        assert row[2] == count == size_plan.bar_count
        assert row[3] == pytest.approx(stock) == size_plan.total_stock_mm / 1000
        assert row[4] == pytest.approx(pieces)
        assert row[5] == pytest.approx(reusable)
        assert row[6] == pytest.approx(remnant - reusable)
        assert row[7] == pytest.approx(kerf_loss)
        assert stock == pytest.approx(pieces + kerf_loss + remnant)
    total = summary[th.TOTAL_TEXT]
    assert total[3] == pytest.approx(sum(sp.total_stock_mm for sp in plan.sizes) / 1000)


def test_details_sheet_adds_up_to_input(
    template_output: tuple[CuttingPlan, openpyxl.Workbook],
) -> None:
    _, workbook = template_output
    totals: dict[str, int] = {}
    for r in rows(workbook[th.SHEET_NAMES["details"]]):
        assert r[7] == int(str(r[5])) * int(str(r[6]))
        totals[str(r[3])] = totals.get(str(r[3]), 0) + int(str(r[7]))
    check = rows(workbook[th.SHEET_NAMES["check"]])
    assert totals == {str(r[1]): r[2] for r in check}


def test_purchase_sheet_matches_summary(
    template_output: tuple[CuttingPlan, openpyxl.Workbook],
) -> None:
    plan, workbook = template_output
    purchase = rows(workbook[th.SHEET_NAMES["purchase"]])
    for size_plan in plan.sizes:
        lines = [r for r in purchase if r[0] == size_plan.size]
        subtotal = next(r for r in purchase if r[0] == f"รวม {size_plan.size}")
        assert sum(int(str(r[2])) for r in lines) == subtotal[2] == size_plan.bar_count
        assert sum(float(str(r[3])) for r in lines) == pytest.approx(float(str(subtotal[3])))
        assert sum(float(str(r[4])) for r in lines) == pytest.approx(float(str(subtotal[4])))
    grand = purchase[-1]
    assert grand[0] == th.TOTAL_TEXT
    assert grand[2] == sum(sp.bar_count for sp in plan.sizes)


# --- check sheet recounts (B8) ------------------------------------------------------


def test_check_sheet_recounts_from_the_plan(tmp_path: Path) -> None:
    pieces = [Piece("DB12", "1", 3000, 3, (2,)), Piece("DB12", "2", 13000, 1, (3,))]
    plan = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    lying = [replace(pieces[0], quantity=4), pieces[1]]
    table = InputTable(headers=(), row_numbers=(), rows=())
    path = write_plan(tmp_path / "o.xlsx", plan, WriteSource(lying, table), FIXED_TIME)
    check = rows(openpyxl.load_workbook(path)[th.SHEET_NAMES["check"]])
    assert check == [
        ("DB12", "1", 4, 3, 0, th.CHECK_FAIL),
        ("DB12", "2", 1, 0, 1, th.CHECK_OK),
    ]


# --- remnants, unplaced, empty sheets ---------------------------------------------------


def test_empty_sheets_say_none(tmp_path: Path) -> None:
    _, workbook = plan_and_write(
        tmp_path, [Piece("DB12", "1", 6000, 2, (2,))], OptimizerSettings(stock_lengths_mm=(12000,))
    )
    for key in ("remnants", "unplaced"):
        assert rows(workbook[th.SHEET_NAMES[key]]) == [
            (th.NONE_TEXT,) + (None,) * (len(th.HEADERS[key]) - 1)
        ]


def test_unplaced_and_remnant_rows(tmp_path: Path) -> None:
    pieces = [
        Piece("DB12", "1", 13000, 2, (2,)),
        Piece("DB12", "2", 7000, 1, (3,)),
        Piece("DB12", "3", 2000, 1, (4,)),
    ]
    _, workbook = plan_and_write(tmp_path, pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    unplaced = rows(workbook[th.SHEET_NAMES["unplaced"]])
    assert unplaced == [("DB12", "1", 13.0, 2, "ยาวกว่าเหล็กเส้นที่ยาวที่สุด (12.000 ม.)")]
    # 12 - 7 - 2 = 3 m left; the 2 m piece length fits it -> reusable, longest cuttable 2 m
    assert rows(workbook[th.SHEET_NAMES["remnants"]]) == [("DB12", 3.0, 1, 2.0)]
    plan_row = rows(workbook[th.SHEET_NAMES["plan"]])[0]
    assert plan_row[9:] == (th.CHECK_OK, 2.0)


def test_waste_row_has_no_tick(tmp_path: Path) -> None:
    pieces = [Piece("DB12", "1", 5000, 2, (2,))]
    _, workbook = plan_and_write(tmp_path, pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    plan_row = rows(workbook[th.SHEET_NAMES["plan"]])[0]
    assert plan_row[8] == 2.0  # 2 m remnant, shorter than every piece (5 m) -> waste
    assert plan_row[9:] == (None, None)


# --- weights ------------------------------------------------------------------------


def test_purchase_weight_db12(tmp_path: Path) -> None:
    pieces = [Piece("DB12", "1", 12000, 1, (2,))]
    _, workbook = plan_and_write(tmp_path, pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    purchase = rows(workbook[th.SHEET_NAMES["purchase"]])
    assert purchase[0][4] == pytest.approx(10.67, abs=0.005)


def test_unknown_size_weight_left_blank_with_warning(tmp_path: Path) -> None:
    pieces = [Piece("ปลอก", "1", 1000, 1, (2,))]
    _, workbook = plan_and_write(tmp_path, pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    summary = rows(workbook[th.SHEET_NAMES["summary"]])
    assert summary[0][9:12] == (None, None, None)
    status_text = [str(r[1]) for r in rows(workbook[th.SHEET_NAMES["status"]])]
    assert any("ปลอก" in t and "น้ำหนัก" in t for t in status_text)


# --- status and input sheets -------------------------------------------------------------


def test_status_sheet(template_output: tuple[CuttingPlan, openpyxl.Workbook]) -> None:
    _, workbook = template_output
    status = dict(rows(workbook[th.SHEET_NAMES["status"]])[:9])
    labels = th.STATUS_LABELS_SHEET
    assert str(status[labels["input_file"]]).endswith("t.xlsx")
    assert status[labels["created_at"]] == "2026-10-14 09:30:00"
    assert status[labels["stock_lengths"]] == "10.000, 12.000"
    assert status[labels["stock_mode"]] == "ผสมความยาวได้"
    assert status[labels["kerf"]] == 3
    all_rows = dict(r[:2] for r in rows(workbook[th.SHEET_NAMES["status"]]) if r[0])
    assert all_rows[labels["note"]] == th.REPRODUCIBILITY_NOTE


def test_input_sheet_copies_extra_columns_and_row_numbers(
    make_xlsx: MakeXlsx, tmp_path: Path
) -> None:
    path = make_xlsx(
        [["DB12", 1, 3.5, 2, "ชั้น 1"], [None] * 5, ["DB12", 2, 1.5, 1, "ชั้น 2"]],
        headers=("Size", "No", "Length", "Quantity", "หมายเหตุ"),
    )
    source = read_pieces(path)
    plan = planner.plan(source.pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    out = write_plan(
        tmp_path / "o.xlsx", plan, WriteSource(source.pieces, source.table), FIXED_TIME
    )
    sheet = openpyxl.load_workbook(out)[th.SHEET_NAMES["input"]]
    assert [c.value for c in sheet[1]] == [
        "แถวใน Excel",
        "Size",
        "No",
        "Length",
        "Quantity",
        "หมายเหตุ",
    ]
    assert rows(sheet) == [(2, "DB12", 1, 3.5, 2, "ชั้น 1"), (4, "DB12", 2, 1.5, 1, "ชั้น 2")]


def test_text_starting_with_equals_stays_text(tmp_path: Path) -> None:
    pieces = [Piece("DB12", "1", 1000, 1, (2,))]
    plan = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    table = InputTable(headers=("Size", "หมายเหตุ"), row_numbers=(2,), rows=(("DB12", "=1+1"),))
    out = write_plan(tmp_path / "o.xlsx", plan, WriteSource(pieces, table), FIXED_TIME)
    sheet = openpyxl.load_workbook(out)[th.SHEET_NAMES["input"]]
    assert sheet["C2"].value == "=1+1"
    assert sheet["C2"].data_type == "s"


# --- atomic write -------------------------------------------------------------------


def test_locked_destination_raises_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(*_: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(OutputWriteError) as exc:
        plan_and_write(
            tmp_path,
            [Piece("DB12", "1", 1000, 1, (2,))],
            OptimizerSettings(stock_lengths_mm=(12000,)),
        )
    issue = exc.value.issues[0]
    assert issue.code is IssueCode.OUTPUT_LOCKED
    assert "กรุณาปิดไฟล์ out.xlsx แล้วลองใหม่" in th.format_issue(issue)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_file_open_elsewhere_is_reported_and_retry_works(tmp_path: Path) -> None:
    pieces = [Piece("DB12", "1", 1000, 1, (2,))]
    settings = OptimizerSettings(stock_lengths_mm=(12000,))
    plan_and_write(tmp_path, pieces, settings)
    target = tmp_path / "out.xlsx"
    with target.open("rb"), pytest.raises(OutputWriteError) as exc:
        plan_and_write(tmp_path, pieces, settings)
    assert exc.value.issues[0].code is IssueCode.OUTPUT_LOCKED
    assert [p.name for p in tmp_path.iterdir()] == ["out.xlsx"]
    plan_and_write(tmp_path, pieces, settings)  # same plan, saved again once closed


def test_missing_folder_is_output_failed(tmp_path: Path) -> None:
    plan = planner.plan(
        [Piece("DB12", "1", 1000, 1, (2,))], OptimizerSettings(stock_lengths_mm=(12000,))
    )
    table = InputTable(headers=(), row_numbers=(), rows=())
    with pytest.raises(OutputWriteError) as exc:
        write_plan(tmp_path / "nope" / "o.xlsx", plan, WriteSource((), table))
    assert exc.value.issues[0].code is IssueCode.OUTPUT_FAILED


# --- CLI ----------------------------------------------------------------------------


def test_cli_writes_output_even_with_unplaced(make_xlsx: MakeXlsx, tmp_path: Path) -> None:
    path = make_xlsx([["DB12", 1, 13.0, 1], ["DB12", 2, 3.0, 2]])
    out = tmp_path / "plan.xlsx"
    assert main([str(path), "--stock", "12", "-o", str(out)]) == EXIT_UNPLACED
    workbook = openpyxl.load_workbook(out)
    assert rows(workbook[th.SHEET_NAMES["unplaced"]])[0][:2] == ("DB12", "1")
