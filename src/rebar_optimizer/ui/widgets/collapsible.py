"""A titled section whose body expands and collapses with a slide."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

from rebar_optimizer.ui import animations


class CollapsibleSection(QWidget):
    toggled = Signal(bool)

    def __init__(self, title: str, body: QWidget, expanded: bool = False) -> None:
        super().__init__()
        self.header = QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setAutoRaise(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.toggled.connect(self._on_toggled)
        self.body = body
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.header)
        layout.addWidget(body)
        self.set_expanded(expanded, animate=False)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        self.header.blockSignals(True)
        self.header.setChecked(expanded)
        self.header.blockSignals(False)
        self._apply(expanded, animate)

    def _on_toggled(self, expanded: bool) -> None:
        self._apply(expanded, animate=True)
        self.toggled.emit(expanded)

    def _apply(self, expanded: bool, animate: bool) -> None:
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        if animate:
            animations.slide(self.body, expanded)
        else:
            animations.stop(self.body, "slide")
            self.body.setMaximumHeight(animations.QWIDGETSIZE_MAX)
            self.body.setVisible(expanded)
