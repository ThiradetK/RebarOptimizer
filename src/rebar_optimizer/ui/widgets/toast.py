"""A short message in the bottom-right corner that fades in and away by itself."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from rebar_optimizer.ui import animations

DEFAULT_DURATION_MS = 3000
MARGIN = 16


class Toast(QWidget):
    """Overlay child of ``parent``; never takes focus or blocks input elsewhere."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(420)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addWidget(self.label)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: animations.fade_out(self))
        parent.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, duration_ms: int = DEFAULT_DURATION_MS) -> None:
        self.label.setText(text)
        self.adjustSize()
        self._reposition()
        self.raise_()
        animations.fade_in(self)
        self._timer.start(duration_ms)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.move(
            parent.width() - self.width() - MARGIN,
            parent.height() - self.height() - MARGIN,
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._reposition()
        return False
