"""Small, non-blocking UI animations and the switch that turns them off (6.3).

Animations run only when all of these allow it: the user setting (Advanced
section), the Windows "Show animations in Windows" setting, and the test
override. Every helper has an instant path that ends in exactly the same
widget state, so turning animations off never changes the result.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QPropertyAnimation,
    QVariantAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QProgressBar, QWidget

DURATION_MS = 220
EASING = QEasingCurve.Type.OutCubic
QWIDGETSIZE_MAX = (1 << 24) - 1
_ATTR = "_rebar_animations"


class _Switch:
    user_enabled = True
    forced: bool | None = None
    system_cache: bool | None = None


_switch = _Switch()


def set_user_enabled(value: bool) -> None:
    _switch.user_enabled = value


def force(value: bool | None) -> None:
    """Override every other setting (``None`` removes the override). Used by tests."""
    _switch.forced = value


def system_allows() -> bool:
    """Windows "Show animations in Windows" (SPI_GETCLIENTAREAANIMATION)."""
    if _switch.system_cache is None:
        _switch.system_cache = True
        if sys.platform == "win32":
            import ctypes  # noqa: PLC0415 - Windows only

            value = ctypes.c_int(1)
            spi_getclientareaanimation = 0x1042
            if ctypes.windll.user32.SystemParametersInfoW(
                spi_getclientareaanimation, 0, ctypes.byref(value), 0
            ):
                _switch.system_cache = bool(value.value)
    return _switch.system_cache


def enabled() -> bool:
    if _switch.forced is not None:
        return _switch.forced
    return _switch.user_enabled and system_allows()


# --- bookkeeping -------------------------------------------------------------------------


def _running(owner: QObject) -> dict[str, QAbstractAnimation]:
    animations: dict[str, QAbstractAnimation] | None = getattr(owner, _ATTR, None)
    if animations is None:
        animations = {}
        setattr(owner, _ATTR, animations)
    return animations


def stop(owner: QObject, key: str) -> None:
    animation = _running(owner).pop(key, None)
    if animation is not None:
        animation.stop()
        animation.deleteLater()


def is_animating(owner: QObject) -> bool:
    return any(a.state() == QAbstractAnimation.State.Running for a in _running(owner).values())


def _start(owner: QObject, key: str, animation: QAbstractAnimation) -> None:
    """Replace any running animation of this kind on ``owner`` and start ``animation``."""
    stop(owner, key)
    running = _running(owner)
    running[key] = animation

    def cleanup() -> None:
        if running.get(key) is animation:
            del running[key]
            animation.deleteLater()

    animation.finished.connect(cleanup)
    animation.start()


def _property_animation(target: QObject, name: bytes, parent: QObject) -> QPropertyAnimation:
    animation = QPropertyAnimation(target, name, parent)
    animation.setDuration(DURATION_MS)
    animation.setEasingCurve(EASING)
    return animation


def _clear_effect(widget: QWidget) -> None:
    # None removes the effect at runtime; the PySide6 stubs do not allow it.
    widget.setGraphicsEffect(None)  # type: ignore[arg-type]


# --- helpers ------------------------------------------------------------------------------


def animate_progress(bar: QProgressBar, value: int) -> None:
    """Slide a progress bar to ``value`` (jumps when going backwards)."""
    stop(bar, "progress")
    if not enabled() or value <= bar.value():
        bar.setValue(value)
        return
    animation = _property_animation(bar, b"value", bar)
    animation.setStartValue(bar.value())
    animation.setEndValue(value)
    _start(bar, "progress", animation)


def fade_in(widget: QWidget, duration_ms: int = DURATION_MS) -> None:
    """Show ``widget`` and fade it in; the opacity effect is removed afterwards."""
    stop(widget, "fade")
    widget.show()
    _clear_effect(widget)
    if not enabled():
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    animation = _property_animation(effect, b"opacity", widget)
    animation.setDuration(duration_ms)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.finished.connect(lambda: _clear_effect(widget))
    _start(widget, "fade", animation)


def fade_out(
    widget: QWidget, duration_ms: int = DURATION_MS, then: Callable[[], None] | None = None
) -> None:
    """Fade ``widget`` out, hide it, then call ``then``."""
    stop(widget, "fade")

    def finish() -> None:
        widget.hide()
        _clear_effect(widget)
        if then is not None:
            then()

    if not enabled() or not widget.isVisible():
        finish()
        return
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = _property_animation(effect, b"opacity", widget)
    animation.setDuration(duration_ms)
    animation.setStartValue(1.0)
    animation.setEndValue(0.0)
    animation.finished.connect(finish)
    _start(widget, "fade", animation)


def set_text_faded(label: QLabel, text: str) -> None:
    """Change a label's text with a quick fade-in of the new text."""
    if label.text() == text:
        return
    label.setText(text)
    if label.isVisible():
        fade_in(label)


def slide(widget: QWidget, visible: bool) -> None:
    """Expand or collapse ``widget`` vertically; always ends in plain ``setVisible``."""
    stop(widget, "slide")

    def finish() -> None:
        widget.setMaximumHeight(QWIDGETSIZE_MAX)
        widget.setVisible(visible)

    if not enabled() or (not widget.isHidden()) == visible:
        finish()
        return
    if visible:
        start, end = 0, max(widget.sizeHint().height(), 1)
        widget.setMaximumHeight(0)
        widget.setVisible(True)
    else:
        start, end = widget.height(), 0
    animation = _property_animation(widget, b"maximumHeight", widget)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.finished.connect(finish)
    _start(widget, "slide", animation)


def drive(owner: QObject, key: str, duration_ms: int, on_value: Callable[[float], None]) -> None:
    """Call ``on_value`` with 0..1 over ``duration_ms`` (just 1.0 when disabled)."""
    stop(owner, key)
    if not enabled():
        on_value(1.0)
        return
    animation = QVariantAnimation(owner)
    animation.setDuration(duration_ms)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.valueChanged.connect(lambda value: on_value(float(value)))
    animation.finished.connect(lambda: on_value(1.0))
    _start(owner, key, animation)
