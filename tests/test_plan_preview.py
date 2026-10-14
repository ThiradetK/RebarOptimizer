from __future__ import annotations

import subprocess
import sys
import time

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QPixmap
from pytestqt.qtbot import QtBot

from rebar_optimizer.domain.models import (
    Bar,
    BarGroup,
    Cut,
    InventoryStock,
    Piece,
    SizePlan,
    SolveStatus,
)
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.services import planner
from rebar_optimizer.ui.theme import DARK, LIGHT
from rebar_optimizer.ui.widgets.plan_preview import (
    BAR_HEIGHT,
    ROW_HEIGHT,
    TOP,
    PlanPreview,
    SegmentKind,
    build_rows,
    no_colors,
    stock_label,
)

pytestmark = pytest.mark.gui


def size_plan(
    pieces: list[Piece], stocks: tuple[int, ...] = (12000,), kerf_mm: int = 0
) -> SizePlan:
    settings = OptimizerSettings(stock_lengths_mm=stocks, kerf_mm=kerf_mm, time_limit_s=5)
    return planner.plan(pieces, settings).sizes[0]


def big_plan(rows: int) -> SizePlan:
    groups = tuple(
        BarGroup(
            Bar("DB12", 12000, (Cut(str(i % 40), 3000 + i % 7), Cut(str((i + 1) % 40), 2500))),
            count=1 + i % 9,
        )
        for i in range(rows)
    )
    return SizePlan(
        size="DB12",
        kerf_mm=3,
        groups=groups,
        unplaced=(),
        remnants=(),
        piece_lengths_mm=(2500, 3000),
        status=SolveStatus.GREEDY,
        lower_bound_mm=None,
    )


# --- layout of one bar (D3) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("lengths", "kerf_mm", "expected"),
    [
        # r == 0: n-1 cuts, no remnant
        ([5000, 4997], 3, ["piece", "kerf", "piece"]),
        # 0 < r <= k: last cut eats the rest
        ([5000, 4996], 3, ["piece", "kerf", "piece", "kerf"]),
        # r > k: n cuts and a remnant of 994 mm, shorter than every piece -> waste
        ([5000, 4000], 3, ["piece", "kerf", "piece", "kerf", "waste"]),
    ],
)
def test_segments_follow_the_kerf_rule(
    lengths: list[int], kerf_mm: int, expected: list[str]
) -> None:
    pieces = [Piece("DB12", str(i), length, 1, (i + 2,)) for i, length in enumerate(lengths)]
    plan = size_plan(pieces, (10000,), kerf_mm)
    (row,) = build_rows(plan)
    assert [s.kind.value for s in row.segments] == expected
    assert sum(s.length_mm for s in row.segments) == row.stock_mm == 10000
    starts = [s.start_mm for s in row.segments]
    assert starts == sorted(starts) and starts[0] == 0


def test_reusable_remnant_is_marked() -> None:
    plan = size_plan([Piece("DB12", "1", 7000, 1, (2,)), Piece("DB12", "2", 2000, 1, (3,))])
    (row,) = build_rows(plan)
    remnant = row.segments[-1]
    assert remnant.kind is SegmentKind.REUSABLE
    assert (remnant.length_mm, remnant.longest_cuttable_mm) == (3000, 2000)


def test_tooltip_describes_the_segment_under_the_mouse(qtbot: QtBot) -> None:
    preview = PlanPreview()
    qtbot.addWidget(preview)
    preview.resize(800, 100)
    preview.set_plan(size_plan([Piece("DB12", "7", 6000, 4, (2,))]))
    y = TOP + BAR_HEIGHT // 2 + (ROW_HEIGHT - BAR_HEIGHT) // 2
    x = preview._bar_left() + 10
    text = preview.tooltip_text(QPoint(x, y))
    assert text is not None
    assert "No.7" in text and "6.000" in text and "× 2 เส้น = 4 ชิ้น" in text
    assert preview.tooltip_text(QPoint(x, 500)) is None


# --- colours -------------------------------------------------------------------------


def test_colours_differ_between_numbers_and_follow_the_theme() -> None:
    colours = no_colors(["2", "10", "1"])
    assert len({c.name() for c in colours.values()}) == 3
    assert no_colors(["1", "2", "10"])["10"].name() == colours["10"].name()
    assert no_colors(["1"], DARK)["1"].name() != no_colors(["1"], LIGHT)["1"].name()


def test_colours_are_identical_across_processes() -> None:
    code = (
        "from rebar_optimizer.ui.widgets.plan_preview import no_colors;"
        "print(sorted((k, v.name()) for k, v in no_colors(['A1','B2','5','12']).items()))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={**__import__("os").environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("1", "2")
    }
    assert len(outputs) == 1


# --- painting ------------------------------------------------------------------------


def test_paints_500_rows_quickly(qtbot: QtBot) -> None:
    preview = PlanPreview()
    qtbot.addWidget(preview)
    preview.set_plan(big_plan(500))
    preview.resize(900, preview.sizeHint().height())
    assert preview.height() == TOP * 2 + 500 * ROW_HEIGHT
    pixmap = QPixmap(preview.size())
    started = time.perf_counter()
    preview.render(pixmap)
    elapsed = time.perf_counter() - started
    assert not pixmap.isNull()
    assert elapsed < 0.4, f"painting 500 rows took {elapsed:.3f}s"


def test_empty_plan_paints(qtbot: QtBot) -> None:
    preview = PlanPreview()
    qtbot.addWidget(preview)
    preview.set_plan(None)
    preview.resize(400, 60)
    pixmap = QPixmap(preview.size())
    preview.render(pixmap)
    assert preview.rows() == []


def test_inventory_bars_are_labelled(qtbot: QtBot) -> None:
    pieces = [Piece("DB12", "1", 6000, 3, (2,))]  # 2 on a new 12 m bar, 1 on the 7.3 m bar
    settings = OptimizerSettings(stock_lengths_mm=(12000,), time_limit_s=5)
    plan = planner.plan(pieces, settings, inventory=[InventoryStock("DB12", 7300, 1, (2,))])
    rows = build_rows(plan.sizes[0])
    assert sorted((r.stock_mm, r.inventory) for r in rows) == [(7300, True), (12000, False)]
    labels = {stock_label(r) for r in rows}
    assert labels == {"7.300 ม. คงคลัง", "12.000 ม."}
    preview = PlanPreview()
    qtbot.addWidget(preview)
    preview.set_plan(plan.sizes[0])
    preview.resize(600, preview.sizeHint().height())
    pixmap = QPixmap(preview.size())
    preview.render(pixmap)
    assert not pixmap.isNull()
