"""Colours, fonts and the stylesheet, in one place (6.3)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

FONT_FAMILIES = ["Segoe UI", "Leelawadee UI", "Tahoma"]
"""Segoe UI first; Leelawadee UI covers Thai glyphs on Windows."""
FONT_POINT_SIZE = 10


@dataclass(frozen=True)
class Theme:
    dark: bool
    window: str
    surface: str
    surface_alt: str
    text: str
    text_muted: str
    border: str
    accent: str
    accent_text: str
    danger: str
    danger_surface: str
    warning_surface: str
    warning_text: str
    success: str


LIGHT = Theme(
    dark=False,
    window="#f4f5f7",
    surface="#ffffff",
    surface_alt="#eef0f3",
    text="#1f2328",
    text_muted="#5c6370",
    border="#d0d4da",
    accent="#1f6feb",
    accent_text="#ffffff",
    danger="#c62828",
    danger_surface="#fdecea",
    warning_surface="#fff4e5",
    warning_text="#8a4b00",
    success="#2e7d32",
)

DARK = Theme(
    dark=True,
    window="#1e1f22",
    surface="#2b2d31",
    surface_alt="#33363b",
    text="#e6e7e9",
    text_muted="#a3a8b0",
    border="#44484f",
    accent="#4c8dff",
    accent_text="#ffffff",
    danger="#ef6b6b",
    danger_surface="#3a2224",
    warning_surface="#3a2e1a",
    warning_text="#ffcc80",
    success="#6fcf73",
)


def system_prefers_dark() -> bool:
    hints = QGuiApplication.styleHints()
    return hints.colorScheme() == Qt.ColorScheme.Dark


def current_theme() -> Theme:
    return DARK if system_prefers_dark() else LIGHT


def app_font() -> QFont:
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSize(FONT_POINT_SIZE)
    return font


def stylesheet(theme: Theme) -> str:
    t = theme
    return f"""
QWidget {{ color: {t.text}; }}
QMainWindow, QDialog {{ background: {t.window}; }}
QGroupBox {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
    margin-top: 20px; padding: 12px 10px 8px 10px; font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; top: 2px; padding: 2px 4px; color: {t.text_muted};
}}
QLineEdit, QPlainTextEdit, QTableWidget, QListWidget {{
    background: {t.surface}; border: 1px solid {t.border};
    border-radius: 6px; padding: 4px 6px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit[invalid="true"] {{ border: 1px solid {t.danger}; background: {t.danger_surface}; }}
QLabel#fieldError {{ color: {t.danger}; }}
QLabel#muted {{ color: {t.text_muted}; }}
QPushButton {{
    background: {t.surface_alt}; border: 1px solid {t.border};
    border-radius: 6px; padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {t.accent}; }}
QPushButton:disabled {{ color: {t.text_muted}; }}
QPushButton#primary {{
    background: {t.accent}; color: {t.accent_text}; border: none; font-weight: 600;
}}
QPushButton#primary:disabled {{ background: {t.border}; color: {t.text_muted}; }}
QPushButton#danger {{ color: {t.danger}; }}
QProgressBar {{
    background: {t.surface_alt}; border: none; border-radius: 4px; height: 8px; text-align: center;
}}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 4px; }}
QFrame#warningBanner {{
    background: {t.warning_surface}; border: 1px solid {t.warning_text}; border-radius: 8px;
}}
QFrame#warningBanner QLabel {{ color: {t.warning_text}; }}
QFrame#summaryCard {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px; }}
QLabel#metricValue {{ font-size: 15pt; font-weight: 600; }}
QLabel#metricLabel {{ color: {t.text_muted}; }}
QHeaderView::section {{
    background: {t.surface_alt}; border: none;
    border-bottom: 1px solid {t.border}; padding: 4px 6px;
}}
"""


def apply_theme(app: QApplication, theme: Theme | None = None) -> Theme:
    """Fusion style, our font, palette and stylesheet. Returns the theme used."""
    theme = theme or current_theme()
    app.setStyle("Fusion")
    app.setFont(app_font())
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.window))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.surface))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.surface_alt))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.surface_alt))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.accent))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.accent_text))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(theme))
    return theme
