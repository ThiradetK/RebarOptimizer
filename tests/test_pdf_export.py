from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from pytestqt.qtbot import QtBot

from rebar_optimizer.domain.errors import IssueCode, OutputWriteError
from rebar_optimizer.domain.models import CuttingPlan, Piece
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.services import planner
from rebar_optimizer.ui.pdf_export import (
    DPI,
    ROW_MM,
    SUMMARY_MM,
    CuttingSheet,
    body_mm,
    paginate,
    pdf_piece_labels,
    piece_entries,
    summary_lines,
    table_columns,
    write_pdf,
)
from rebar_optimizer.ui.widgets.plan_preview import SegmentKind, prepare_preview

pytestmark = pytest.mark.gui


def make_plan(rows_per_size: int = 3) -> CuttingPlan:
    pieces = [Piece("DB12", f"{i}", 1000 + 700 * i, 3, (i + 2,)) for i in range(rows_per_size)] + [
        Piece("DB16", "A1", 13000, 1, (90,)),
        Piece("DB16", "A2", 2500, 5, (91,)),
    ]
    return planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(10000, 12000), kerf_mm=3))


def pdf_page_count(path: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", path.read_bytes()))


# --- pagination ---------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    blocks=st.lists(
        st.tuples(st.integers(0, 120), st.integers(0, 200), st.integers(1, 7)),
        min_size=1,
        max_size=4,
    ),
    paper=st.sampled_from(["A4", "A3"]),
)
def test_every_row_and_entry_is_on_exactly_one_page(
    blocks: list[tuple[int, int, int]], paper: str
) -> None:
    body = body_mm(paper)
    pages = paginate(blocks, body)
    for size_index, (n_rows, n_entries, _) in enumerate(blocks):
        own = [p for p in pages if p.size_index == size_index]
        assert own[0].summary and not any(p.summary for p in own[1:])
        assert [r for p in own for r in p.rows] == list(range(n_rows))
        assert [e for p in own for e in p.entries] == list(range(n_entries))
        assert sum(p.table_title for p in own) == (1 if n_entries else 0)
    assert all(p.used_mm <= body + 1e-9 for p in pages)
    # sizes appear in order, each starting on a new page
    assert [p.size_index for p in pages] == sorted(p.size_index for p in pages)


def test_rows_fill_the_page_before_breaking() -> None:
    first_page_rows = int((body_mm("A4") - SUMMARY_MM) // ROW_MM)
    pages = paginate([(first_page_rows + 1, 0, 1)], body_mm("A4"))
    assert [len(p.rows) for p in pages] == [first_page_rows, 1]
    assert len(paginate([(40, 0, 1)], body_mm("A3"))) < len(paginate([(40, 0, 1)], body_mm("A4")))


def test_long_numbers_get_wider_table_columns() -> None:
    assert table_columns("A4", 10) > table_columns("A4", 40) >= 1
    assert table_columns("A3", 10) > table_columns("A4", 10)
    assert table_columns("A4", 500) == 1


# --- content ------------------------------------------------------------------------------


def test_piece_entries_count_every_cut() -> None:
    plan = make_plan()
    for size_plan in plan.sizes:
        entries = piece_entries(prepare_preview(size_plan).rows)
        cut = sum(len(g.bar.cuts) * g.count for g in size_plan.groups)
        assert sum(n for _, _, n in entries) == cut


def test_labels_carry_the_length_and_summary_names_unplaced() -> None:
    plan = make_plan()
    row = prepare_preview(plan.sizes[0]).rows[0]
    piece = next(s for s in row.segments if s.kind is SegmentKind.PIECE)
    assert pdf_piece_labels(piece)[0] == f"No.{piece.no} · {piece.length_mm / 1000:.3f} ม."
    lines = summary_lines(plan.sizes[1])
    assert "เส้น" in lines[0] and "No.A1 13.000 ม. × 1" in lines[1]


# --- files --------------------------------------------------------------------------------


@pytest.mark.parametrize("paper", ["A4", "A3"])
def test_pdf_is_written_with_the_planned_pages(qtbot: QtBot, tmp_path: Path, paper: str) -> None:
    plan = make_plan(rows_per_size=12)
    previews = [prepare_preview(sp) for sp in plan.sizes]
    out = tmp_path / "sheet.pdf"
    assert write_pdf(out, plan, previews, "ผลลัพธ์ของ t.xlsx", paper) == out
    assert out.read_bytes().startswith(b"%PDF")
    assert pdf_page_count(out) == len(CuttingSheet(plan, previews, "", paper).pages)
    assert not list(tmp_path.glob("~rebar_*"))  # temporary file cleaned up


def test_pages_draw_on_an_image(qtbot: QtBot) -> None:
    plan = make_plan()
    sheet = CuttingSheet(plan, [prepare_preview(sp) for sp in plan.sizes], "x", "A4")
    width, height = sheet.page_size_px()
    image = QImage(width // 4, height // 4, QImage.Format.Format_RGB32)
    image.setDotsPerMeterX(round(DPI / 4 / 0.0254))
    image.setDotsPerMeterY(round(DPI / 4 / 0.0254))
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.scale(0.25, 0.25)
    sheet.draw(painter, 0)
    painter.end()
    colours = {
        image.pixel(x, y) for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)
    }
    assert len(colours) > 5  # bars, text and boxes were drawn


def test_locked_pdf_reports_output_locked(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = make_plan()

    def locked(*_: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(OutputWriteError) as info:
        write_pdf(tmp_path / "s.pdf", plan, [prepare_preview(sp) for sp in plan.sizes], "", "A4")
    assert info.value.issues[0].code is IssueCode.OUTPUT_LOCKED
