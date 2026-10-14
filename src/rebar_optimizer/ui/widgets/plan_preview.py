"""Cutting plan drawn as coloured bars, one row per ``BarGroup`` (6.3).

Painted with ``QPainter`` straight from the ``SizePlan`` (never from Excel).
Only rows inside the exposed rectangle are painted, so hundreds of rows stay
fast inside a ``QScrollArea``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QHelpEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from rebar_optimizer.domain.models import SizePlan, StockSource
from rebar_optimizer.i18n.th import GUI_TEXT, format_metres
from rebar_optimizer.services.planner import natural_key
from rebar_optimizer.services.summary import bar_detail
from rebar_optimizer.ui import animations
from rebar_optimizer.ui.theme import LIGHT, Theme

ROW_HEIGHT = 28
BAR_HEIGHT = 18
TOP = 8
LEFT_LABEL = 56
RIGHT_LABEL = 78
GAP = 8
GROW_TOTAL_MS = 600
GROW_ROW_MS = 300
GROW_ROWS = 30
"""Only the first rows grow in; the rest appear at once (6.3)."""
_STAGGER_MS = (GROW_TOTAL_MS - GROW_ROW_MS) / (GROW_ROWS - 1)
_EASING = QEasingCurve(QEasingCurve.Type.OutCubic)


class SegmentKind(Enum):
    PIECE = "piece"
    KERF = "kerf"
    REUSABLE = "reusable"
    WASTE = "waste"


@dataclass(frozen=True)
class Segment:
    kind: SegmentKind
    start_mm: int
    length_mm: int
    no: str = ""
    per_bar: int = 0
    longest_cuttable_mm: int = 0


@dataclass(frozen=True)
class Row:
    count: int
    stock_mm: int
    segments: tuple[Segment, ...]
    inventory: bool = False
    """The bar comes from inventory (phase 8), not purchased."""


def build_rows(plan: SizePlan) -> list[Row]:
    """Lay out each bar: piece, kerf, piece, ..., [last kerf], remnant (D3)."""
    rows: list[Row] = []
    k = plan.kerf_mm
    for group in plan.groups:
        detail = bar_detail(group, k, plan.piece_lengths_mm)
        usage = detail.usage
        per_no: dict[str, int] = {}
        for cut in group.bar.cuts:
            per_no[cut.no] = per_no.get(cut.no, 0) + 1
        segments: list[Segment] = []
        position = 0
        n = len(group.bar.cuts)
        for index, cut in enumerate(group.bar.cuts):
            segments.append(
                Segment(SegmentKind.PIECE, position, cut.length_mm, cut.no, per_no[cut.no])
            )
            position += cut.length_mm
            if index < n - 1 and k:
                segments.append(Segment(SegmentKind.KERF, position, k))
                position += k
        last_kerf = usage.kerf_loss_mm - k * (n - 1)
        if last_kerf > 0:
            segments.append(Segment(SegmentKind.KERF, position, last_kerf))
            position += last_kerf
        if usage.remnant_mm > 0:
            kind = SegmentKind.REUSABLE if detail.is_reusable else SegmentKind.WASTE
            segments.append(
                Segment(
                    kind,
                    position,
                    usage.remnant_mm,
                    longest_cuttable_mm=detail.longest_cuttable_mm or 0,
                )
            )
        rows.append(
            Row(
                group.count,
                group.bar.stock_length_mm,
                tuple(segments),
                inventory=group.bar.source is StockSource.INVENTORY,
            )
        )
    return rows


def stock_label(row: Row) -> str:
    key = "preview_stock_inventory" if row.inventory else "preview_stock"
    return GUI_TEXT[key].format(stock_m=format_metres(row.stock_mm))


# Distinct, non-green hues (green means a reusable remnant): light / dark variants.
PALETTE_LIGHT = (
    "#7fa8f5",
    "#f5a65b",
    "#c79bf2",
    "#f2839a",
    "#5fc6d9",
    "#ecd060",
    "#9aa3f7",
    "#f7b8a1",
    "#7ec0f2",
    "#e59ae0",
    "#d9b98c",
    "#e0796b",
)
PALETTE_DARK = (
    "#4f78c4",
    "#c27a33",
    "#9168c2",
    "#c0566e",
    "#2f97aa",
    "#b49c2f",
    "#6a72c7",
    "#c08670",
    "#4b90c2",
    "#b26aad",
    "#a6875c",
    "#b04a3d",
)


def no_colors(nos: Iterable[str], theme: Theme = LIGHT) -> dict[str, QColor]:
    """Colour per No: palette slots in natural No order, so the same input always
    gets the same colours and neighbouring numbers never share one (up to 12)."""
    palette = PALETTE_DARK if theme.dark else PALETTE_LIGHT
    ordered = sorted(set(nos), key=natural_key)
    return {no: QColor(palette[i % len(palette)]) for i, no in enumerate(ordered)}


class PlanPreview(QWidget):
    def __init__(self, theme: Theme = LIGHT, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._rows: list[Row] = []
        self._colors: dict[str, QColor] = {}
        self._max_stock = 1
        self._grow = 1.0
        self._right_label = RIGHT_LABEL
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setMinimumWidth(LEFT_LABEL + RIGHT_LABEL + 120)

    # --- data ------------------------------------------------------------------------

    def set_plan(self, plan: SizePlan | None, max_stock_mm: int | None = None) -> None:
        self._rows = build_rows(plan) if plan is not None else []
        self._colors = no_colors(
            (seg.no for row in self._rows for seg in row.segments if seg.no), self._theme
        )
        stocks = [row.stock_mm for row in self._rows]
        self._max_stock = max_stock_mm or (max(stocks) if stocks else 1)
        # Inventory labels ("7.300 ม. คงคลัง") are wider than the plain length.
        metrics = self.fontMetrics()
        self._right_label = max(
            [RIGHT_LABEL] + [metrics.horizontalAdvance(stock_label(row)) + 4 for row in self._rows]
        )
        self.updateGeometry()
        self.setMinimumHeight(self.sizeHint().height())
        animations.drive(self, "grow", GROW_TOTAL_MS, self._set_grow)
        self.update()

    def rows(self) -> list[Row]:
        return list(self._rows)

    def colors(self) -> dict[str, QColor]:
        return dict(self._colors)

    @property
    def grow(self) -> float:
        return self._grow

    def _set_grow(self, value: float) -> None:
        self._grow = value
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(640, TOP * 2 + max(len(self._rows), 1) * ROW_HEIGHT)

    # --- geometry --------------------------------------------------------------------

    def _bar_left(self) -> int:
        return LEFT_LABEL + GAP

    def _px_per_mm(self) -> float:
        width = self.width() - LEFT_LABEL - self._right_label - 2 * GAP
        return max(width, 10) / self._max_stock

    def _row_fraction(self, index: int) -> float:
        if index >= GROW_ROWS or self._grow >= 1.0:
            return 1.0
        elapsed = self._grow * GROW_TOTAL_MS - index * _STAGGER_MS
        return _EASING.valueForProgress(min(max(elapsed / GROW_ROW_MS, 0.0), 1.0))

    def row_at(self, y: int) -> int | None:
        index = (y - TOP) // ROW_HEIGHT
        return index if 0 <= index < len(self._rows) else None

    def segment_at(self, pos: QPoint) -> tuple[Row, Segment] | None:
        index = self.row_at(pos.y())
        if index is None:
            return None
        row = self._rows[index]
        mm = (pos.x() - self._bar_left()) / self._px_per_mm()
        for segment in row.segments:
            if segment.start_mm <= mm < segment.start_mm + segment.length_mm:
                return row, segment
        return None

    # --- painting --------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        t = self._theme
        painter.fillRect(event.rect(), QColor(t.surface))
        if not self._rows:
            painter.setPen(QColor(t.text_muted))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, GUI_TEXT["preview_empty"])
            return
        first = max(0, (event.rect().top() - TOP) // ROW_HEIGHT)
        last = min(len(self._rows) - 1, (event.rect().bottom() - TOP) // ROW_HEIGHT)
        scale = self._px_per_mm()
        metrics = painter.fontMetrics()
        text_pen = QPen(QColor(t.text))
        muted_pen = QPen(QColor(t.text_muted))
        kerf_color = QColor("#2b2b2b") if not t.dark else QColor("#0d0d0d")
        waste_color = QColor("#b0b4ba") if not t.dark else QColor("#5b5f66")
        reusable_color = QColor(t.success).lighter(150 if not t.dark else 90)
        hatch = QBrush(QColor(t.success).darker(130), Qt.BrushStyle.BDiagPattern)
        border = QPen(QColor(t.border))

        for index in range(first, last + 1):
            row = self._rows[index]
            y = TOP + index * ROW_HEIGHT
            bar_top = y + (ROW_HEIGHT - BAR_HEIGHT) // 2
            painter.setPen(muted_pen)
            painter.drawText(
                QRect(0, y, LEFT_LABEL, ROW_HEIGHT),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                GUI_TEXT["preview_count"].format(count=row.count),
            )
            full_width = row.stock_mm * scale
            visible_width = full_width * self._row_fraction(index)
            left = self._bar_left()
            painter.save()
            painter.setClipRect(QRectF(left, bar_top, visible_width, BAR_HEIGHT))
            kerf_rects: list[QRectF] = []
            for segment in row.segments:
                x = left + segment.start_mm * scale
                w = max(segment.length_mm * scale, 1.0)
                rect = QRectF(x, bar_top, w, BAR_HEIGHT)
                if segment.kind is SegmentKind.PIECE:
                    painter.fillRect(rect, self._colors[segment.no])
                    label = f"No.{segment.no}"
                    if metrics.horizontalAdvance(label) + 6 < w:
                        painter.setPen(QPen(QColor("#1f2328")))
                        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
                elif segment.kind is SegmentKind.KERF:
                    kerf_rects.append(rect)
                elif segment.kind is SegmentKind.REUSABLE:
                    painter.fillRect(rect, reusable_color)
                    painter.fillRect(rect, hatch)
                else:
                    painter.fillRect(rect, waste_color)
            # Kerf lines last: a 3 mm cut is often narrower than a pixel and would
            # otherwise be painted over by the next piece.
            for rect in kerf_rects:
                painter.fillRect(rect, kerf_color)
            painter.setPen(border)
            painter.drawRect(QRectF(left, bar_top, full_width, BAR_HEIGHT))
            painter.restore()
            painter.setPen(text_pen)
            painter.drawText(
                QRect(int(left + full_width) + GAP, y, self._right_label, ROW_HEIGHT),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                stock_label(row),
            )

    # --- tooltip ---------------------------------------------------------------------

    def tooltip_text(self, pos: QPoint) -> str | None:
        hit = self.segment_at(pos)
        if hit is None:
            return None
        row, segment = hit
        if segment.kind is SegmentKind.PIECE:
            return GUI_TEXT["preview_piece"].format(
                no=segment.no,
                length_m=format_metres(segment.length_mm),
                per_bar=segment.per_bar,
                count=row.count,
                total=segment.per_bar * row.count,
            )
        if segment.kind is SegmentKind.KERF:
            return GUI_TEXT["preview_kerf"].format(length_mm=segment.length_mm)
        if segment.kind is SegmentKind.REUSABLE:
            return GUI_TEXT["preview_reusable"].format(
                length_m=format_metres(segment.length_mm),
                longest_m=format_metres(segment.longest_cuttable_mm),
                count=row.count,
            )
        return GUI_TEXT["preview_waste"].format(
            length_m=format_metres(segment.length_mm), count=row.count
        )

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_text(event.pos())
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)
