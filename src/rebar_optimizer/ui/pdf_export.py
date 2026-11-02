"""Cutting plan as a printable PDF for the people at the saw.

Every size starts on a new page: a summary of the bars to take, one row per
bar group (tick box, how many identical bars, the bar drawn to scale with No
and length on each piece, stock length), then a No / length / quantity table
for pieces too narrow to label. Always printed in the light theme.

Drawn with ``QPdfWriter`` (no extra dependency) and the same ``paint_bar`` as
the on-screen preview, so paper and screen always agree. Uses no widgets, so it
runs on a worker thread.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
)

from rebar_optimizer import __version__
from rebar_optimizer.domain.models import CuttingPlan, SizePlan
from rebar_optimizer.i18n.th import GUI_TEXT, format_metres, format_metres_short
from rebar_optimizer.io.writer import save_atomically
from rebar_optimizer.services.planner import natural_key
from rebar_optimizer.services.summary import summarize_size
from rebar_optimizer.ui.theme import FONT_FAMILIES, LIGHT
from rebar_optimizer.ui.widgets.plan_preview import (
    PreviewData,
    Row,
    Segment,
    SegmentKind,
    bar_style,
    paint_bar,
    slots_to_colors,
    stock_label,
)

_PAGE_IDS = {"A4": QPageSize.PageSizeId.A4, "A3": QPageSize.PageSizeId.A3}
_PAPER_MM = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}
DPI = 300
MARGIN_MM = 10.0
HEADER_MM = 13.0
FOOTER_MM = 7.0
SUMMARY_MM = 12.0
ROW_MM = 9.0
BAR_MM = 6.0
TABLE_TITLE_MM = 8.0
ENTRY_MM = 5.0
ENTRY_NO_MIN_MM = 14.0
ENTRY_REST_MM = 37.0
"""Table column width besides the No: swatch, length, quantity and gaps."""
CHECK_MM = 4.5
COUNT_RIGHT_MM = 22.0
"""Right edge of the "×n" column, from the left margin."""
BAR_LEFT_MM = 25.0
STOCK_LABEL_MM = 30.0
SUMMARY_ITEMS_MAX = 8
"""Unplaced pieces named in the summary; the rest are counted."""


# --- pagination (pure: no Qt) ------------------------------------------------------------


def body_mm(paper: str) -> float:
    """Height between header and footer."""
    return _PAPER_MM[paper][1] - 2 * MARGIN_MM - HEADER_MM - FOOTER_MM


def table_columns(paper: str, no_width_mm: float) -> int:
    """Columns of the No / length / quantity table when the widest No is
    ``no_width_mm`` (long Nos get fewer, wider columns: they are never cut off)."""
    content = _PAPER_MM[paper][0] - 2 * MARGIN_MM
    return max(1, int(content // (max(no_width_mm, ENTRY_NO_MIN_MM) + ENTRY_REST_MM)))


@dataclass
class Page:
    size_index: int
    summary: bool
    """First page of the size: show what to take from stock."""
    rows: range = field(default_factory=lambda: range(0))
    entries: range = field(default_factory=lambda: range(0))
    table_title: bool = False
    used_mm: float = 0.0


def paginate(blocks: Sequence[tuple[int, int, int]], body: float) -> list[Page]:
    """Split ``(bar rows, table entries, table columns)`` per size into pages of
    ``body`` mm. Each size starts a new page; its table follows the rows, on the
    same page if it fits."""
    pages: list[Page] = []
    for size_index, (n_rows, n_entries, columns) in enumerate(blocks):
        page = Page(size_index, summary=True, used_mm=SUMMARY_MM)

        def next_page(current: Page, index: int = size_index) -> Page:
            pages.append(current)
            return Page(index, summary=False)

        done = 0
        while done < n_rows:
            fit = int((body - page.used_mm) // ROW_MM)
            if fit <= 0:
                page = next_page(page)
                continue
            take = min(fit, n_rows - done)
            page.rows = range(done, done + take)
            page.used_mm += take * ROW_MM
            done += take
            if done < n_rows:
                page = next_page(page)
        done = 0
        while done < n_entries:
            title_mm = TABLE_TITLE_MM if done == 0 else 0.0
            lines = int((body - page.used_mm - title_mm) // ENTRY_MM)
            if lines <= 0:
                page = next_page(page)
                continue
            take = min(lines * columns, n_entries - done)
            page.entries = range(done, done + take)
            page.table_title = done == 0
            page.used_mm += title_mm + math.ceil(take / columns) * ENTRY_MM
            done += take
            if done < n_entries:
                page = next_page(page)
        pages.append(page)
    return pages


def piece_entries(rows: Sequence[Row]) -> list[tuple[str, int, int]]:
    """``(no, length_mm, pieces)`` over all bars, in natural No order."""
    counts: Counter[tuple[str, int]] = Counter()
    for row in rows:
        for segment in row.segments:
            if segment.kind is SegmentKind.PIECE:
                counts[(segment.no, segment.length_mm)] += row.count
    return [
        (no, length, n)
        for (no, length), n in sorted(counts.items(), key=lambda e: (natural_key(e[0][0]), e[0][1]))
    ]


def pdf_piece_labels(segment: Segment) -> Sequence[str]:
    """Longest label that fits: cutters need the length, not only the No."""
    m = format_metres(segment.length_mm)
    return (
        f"No.{segment.no} · {m} ม.",
        f"No.{segment.no} · {m}",
        f"{segment.no} · {m}",
        segment.no,
    )


def summary_lines(plan: SizePlan) -> list[str]:
    summary = summarize_size(plan)

    def items(by_stock: dict[int, int]) -> str:
        return ", ".join(
            GUI_TEXT["pdf_bar_item"].format(length_m=format_metres_short(length), count=f"{n:,}")
            for length, n in by_stock.items()
        )

    parts = []
    if summary.bars_by_stock:
        parts.append(GUI_TEXT["pdf_new_bars"].format(items=items(summary.bars_by_stock)))
    if summary.inventory_by_stock:
        parts.append(GUI_TEXT["pdf_inventory_bars"].format(items=items(summary.inventory_by_stock)))
    parts.append(
        GUI_TEXT["pdf_totals"].format(
            bars=f"{summary.bar_count:,}", pieces=f"{summary.piece_count:,}"
        )
    )
    lines = ["   ·   ".join(parts)]
    if plan.unplaced:
        named = [
            GUI_TEXT["pdf_unplaced_item"].format(
                no=u.no, length_m=format_metres(u.length_mm), quantity=u.quantity
            )
            for u in plan.unplaced[:SUMMARY_ITEMS_MAX]
        ]
        more = len(plan.unplaced) - SUMMARY_ITEMS_MAX
        if more > 0:
            named.append(GUI_TEXT["oversize_more"].format(count=more))
        lines.append(GUI_TEXT["pdf_unplaced"].format(items=", ".join(named)))
    return lines


# --- drawing -------------------------------------------------------------------------------


def _font(point_size: float, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSizeF(point_size)
    font.setBold(bold)
    return font


def _text_width_mm(texts: Iterable[str], font: QFont) -> float:
    """Widest of ``texts`` as printed (measured at the PDF resolution)."""
    device = QImage(1, 1, QImage.Format.Format_RGB32)
    device.setDotsPerMeterX(round(DPI / 0.0254))
    device.setDotsPerMeterY(round(DPI / 0.0254))
    metrics = QFontMetricsF(font, device)
    return max((metrics.horizontalAdvance(t) for t in texts), default=0.0) * 25.4 / DPI


def _entry_no(no: str) -> str:
    return f"No.{no}"


@dataclass(frozen=True)
class _SizeData:
    plan: SizePlan
    rows: tuple[Row, ...]
    colors: dict[str, QColor]
    entries: list[tuple[str, int, int]]
    no_width_mm: float
    columns: int


class _Renderer:
    def __init__(self, painter: QPainter, sheet: CuttingSheet) -> None:
        self.p = painter
        self.k = DPI / 25.4  # device dots per mm
        paper = sheet.paper
        self.width_mm, self.height_mm = _PAPER_MM[paper]
        self.content_mm = self.width_mm - 2 * MARGIN_MM
        self.longest = sheet.longest
        self.header_text = f"{sheet.source_text}   ·   {sheet.printed}"
        self.style = bar_style(LIGHT, hatch_scale=DPI / 96)
        self.text = QColor(LIGHT.text)
        self.muted = QColor(LIGHT.text_muted)

    def rect(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float) -> QRectF:
        k = self.k
        return QRectF(x_mm * k, y_mm * k, w_mm * k, h_mm * k)

    def text_at(
        self,
        rect: QRectF,
        text: str,
        font: QFont,
        color: QColor,
        align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    ) -> None:
        self.p.setFont(font)
        self.p.setPen(QPen(color))
        elided = self.p.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, int(rect.width())
        )
        self.p.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, elided)

    def page(self, page: Page, data: _SizeData, number: int, total: int) -> None:
        x0, y = MARGIN_MM, MARGIN_MM
        title = GUI_TEXT["pdf_title"].format(size=data.plan.size)
        self.text_at(self.rect(x0, y, self.content_mm * 0.7, 7), title, _font(14, True), self.text)
        self.text_at(
            self.rect(x0, y, self.content_mm, 7),
            GUI_TEXT["pdf_page"].format(page=number, pages=total),
            _font(10),
            self.text,
            Qt.AlignmentFlag.AlignRight,
        )
        self.text_at(
            self.rect(x0, y + 7, self.content_mm, 4.5),
            self.header_text,
            _font(8),
            self.muted,
        )
        rule = self.rect(x0, y + HEADER_MM - 1, self.content_mm, 0)
        self.p.setPen(QPen(QColor(LIGHT.border), self.k * 0.3))
        self.p.drawLine(rule.topLeft(), rule.topRight())
        y += HEADER_MM
        if page.summary:
            for index, line in enumerate(summary_lines(data.plan)):
                color = QColor(LIGHT.danger) if index else self.text
                self.text_at(
                    self.rect(x0, y + index * 5.5, self.content_mm, 5.5),
                    line,
                    _font(10, index == 0),
                    color,
                )
            y += SUMMARY_MM
        for index in page.rows:
            self.bar_row(data, data.rows[index], x0, y)
            y += ROW_MM
        if page.entries:
            if page.table_title:
                self.text_at(
                    self.rect(x0, y + 2, self.content_mm, TABLE_TITLE_MM - 2),
                    GUI_TEXT["pdf_table_title"].format(size=data.plan.size),
                    _font(10, True),
                    self.text,
                )
                y += TABLE_TITLE_MM
            self.entries(data, page.entries, x0, y)
        self.footer()

    def bar_row(self, data: _SizeData, row: Row, x0: float, y: float) -> None:
        k = self.k
        top = y + (ROW_MM - BAR_MM) / 2
        self.p.setPen(QPen(self.text, k * 0.25))
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.drawRect(self.rect(x0, y + (ROW_MM - CHECK_MM) / 2, CHECK_MM, CHECK_MM))
        self.text_at(
            self.rect(x0 + CHECK_MM, y, COUNT_RIGHT_MM - CHECK_MM, ROW_MM),
            GUI_TEXT["preview_count"].format(count=row.count),
            _font(11, True),
            self.text,
            Qt.AlignmentFlag.AlignRight,
        )
        bar_space = self.content_mm - BAR_LEFT_MM - STOCK_LABEL_MM
        bar_width = bar_space * row.stock_mm / self.longest
        self.p.setFont(_font(8))
        paint_bar(
            self.p,
            row,
            self.rect(x0 + BAR_LEFT_MM, top, bar_width, BAR_MM),
            data.colors,
            style=self.style,
            labels=pdf_piece_labels,
        )
        self.text_at(
            self.rect(x0 + BAR_LEFT_MM + bar_width + 2, y, STOCK_LABEL_MM, ROW_MM),
            stock_label(row),
            _font(9, True),
            self.text,
        )

    def entries(self, data: _SizeData, entries: range, x0: float, y: float) -> None:
        lines = math.ceil(len(entries) / data.columns)
        no_w = max(data.no_width_mm, ENTRY_NO_MIN_MM) + 1
        for offset, index in enumerate(entries):
            no, length, count = data.entries[index]
            x = x0 + (offset // lines) * (no_w - 1 + ENTRY_REST_MM)
            line_y = y + (offset % lines) * ENTRY_MM
            self.p.fillRect(self.rect(x, line_y + 1, 3, ENTRY_MM - 2), data.colors[no])
            right = Qt.AlignmentFlag.AlignRight
            self.text_at(
                self.rect(x + 4, line_y, no_w, ENTRY_MM), _entry_no(no), _font(9, True), self.text
            )
            metres = f"{format_metres(length)} ม."
            self.text_at(
                self.rect(x + 4 + no_w, line_y, 16, ENTRY_MM), metres, _font(9), self.text, right
            )
            count_text = f"× {count}"
            self.text_at(
                self.rect(x + 20 + no_w, line_y, 11, ENTRY_MM),
                count_text,
                _font(9),
                self.text,
                right,
            )

    def legend_text(self, x: float, y: float, text: str) -> float:
        """Draw a legend label at ``x``; returns where the next item starts."""
        font = _font(8)
        self.p.setFont(font)
        width = self.p.fontMetrics().horizontalAdvance(text) / self.k
        self.text_at(self.rect(x, y, width + 1, 5.5), text, font, self.muted)
        return x + width + 7

    def footer(self) -> None:
        y = self.height_mm - MARGIN_MM - FOOTER_MM + 2
        x = MARGIN_MM
        self.p.setPen(QPen(self.muted, self.k * 0.25))
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.drawRect(self.rect(x, y + 1, 3.5, 3.5))
        x = self.legend_text(x + 5, y, GUI_TEXT["pdf_legend_check"])
        x = self.legend_text(x, y, GUI_TEXT["pdf_legend_count"])
        self.p.fillRect(self.rect(x, y + 1, 0.8, 3.5), self.style.kerf)
        x = self.legend_text(x + 2.5, y, GUI_TEXT["pdf_legend_kerf"])
        swatch = self.rect(x, y + 1, 7, 3.5)
        self.p.fillRect(swatch, self.style.reusable)
        self.p.fillRect(swatch, self.style.hatch)
        x = self.legend_text(x + 9, y, GUI_TEXT["pdf_legend_reusable"])
        self.p.fillRect(self.rect(x, y + 1, 7, 3.5), self.style.waste)
        self.legend_text(x + 9, y, GUI_TEXT["pdf_legend_waste"])


class CuttingSheet:
    """The laid-out document: ``pages`` drawn one at a time onto any painter whose
    device is the paper size at ``DPI`` (a PDF, or an image for checking)."""

    def __init__(
        self,
        plan: CuttingPlan,
        previews: Sequence[PreviewData],
        source_text: str,
        paper: str = "A4",
    ) -> None:
        self.paper = paper
        self.source_text = source_text
        self.printed = GUI_TEXT["pdf_printed"].format(
            time=datetime.now().strftime("%d/%m/%Y %H:%M")
        )
        self.sizes = []
        for size_plan, data in zip(plan.sizes, previews, strict=True):
            entries = piece_entries(data.rows)
            no_width = _text_width_mm({_entry_no(e[0]) for e in entries}, _font(9, True))
            self.sizes.append(
                _SizeData(
                    plan=size_plan,
                    rows=data.rows,
                    colors=slots_to_colors(data.color_slots, LIGHT),
                    entries=entries,
                    no_width_mm=no_width,
                    columns=table_columns(paper, no_width),
                )
            )
        blocks = [(len(s.rows), len(s.entries), s.columns) for s in self.sizes]
        self.pages = paginate(blocks, body_mm(paper))
        self.longest = max((row.stock_mm for s in self.sizes for row in s.rows), default=1)

    def page_size_px(self) -> tuple[int, int]:
        width, height = _PAPER_MM[self.paper]
        return round(width * DPI / 25.4), round(height * DPI / 25.4)

    def draw(self, painter: QPainter, index: int) -> None:
        renderer = _Renderer(painter, self)
        page = self.pages[index]
        renderer.page(page, self.sizes[page.size_index], index + 1, len(self.pages))


def write_pdf(
    path: Path,
    plan: CuttingPlan,
    previews: Sequence[PreviewData],
    source_text: str,
    paper: str = "A4",
) -> Path:
    """Write the PDF; raises ``OutputWriteError`` (e.g. open in a PDF viewer)."""
    path = Path(path)
    sheet = CuttingSheet(plan, previews, source_text, paper)

    def render(target: Path) -> None:
        writer = QPdfWriter(str(target))
        writer.setResolution(DPI)
        writer.setTitle(GUI_TEXT["pdf_document_title"])
        writer.setCreator(f"Rebar Optimizer {__version__}")
        writer.setPageLayout(
            QPageLayout(
                QPageSize(_PAGE_IDS[paper]),
                QPageLayout.Orientation.Landscape,
                QMarginsF(0, 0, 0, 0),
            )
        )
        painter = QPainter(writer)
        try:
            for index in range(len(sheet.pages)):
                if index:
                    writer.newPage()
                sheet.draw(painter, index)
        finally:
            painter.end()
        del painter, writer  # closes the file before it is moved into place

    save_atomically(path, render)
    return path
