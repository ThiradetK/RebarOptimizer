"""Phase 6b: animations, toast, collapsible sections, drag and drop, shortcuts."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QLabel, QWidget
from pytestqt.qtbot import QtBot

from rebar_optimizer.io.template import write_template
from rebar_optimizer.ui import animations
from rebar_optimizer.ui.main_window import MainWindow
from rebar_optimizer.ui.widgets.collapsible import CollapsibleSection
from rebar_optimizer.ui.widgets.toast import Toast

pytestmark = pytest.mark.gui


def settle(qtbot: QtBot, *widgets: QWidget) -> None:
    """Wait until no animation runs on ``widgets`` (or their children)."""

    def idle() -> bool:
        for widget in widgets:
            for obj in (widget, *widget.findChildren(QWidget)):
                if animations.is_animating(obj):
                    return False
        return True

    qtbot.waitUntil(idle, timeout=3_000)


def window_state(win: MainWindow) -> dict[str, object]:
    widgets = {
        "summary": win.summary_card,
        "table": win.size_table,
        "tabs": win.preview_tabs,
        "warnings": win.warnings_list,
        "banner": win.unplaced_banner,
        "advanced": win.advanced_box,
        "log": win.log_view,
    }
    state: dict[str, object] = {}
    for name, widget in widgets.items():
        state[f"{name}.visible"] = widget.isVisible()
        state[f"{name}.effect"] = widget.graphicsEffect() is None
        state[f"{name}.maxh"] = widget.maximumHeight()
    state["progress"] = win.progress_bar.value()
    state["status"] = win.status_label.text()
    state["grow"] = [p.grow for p in win.previews]
    state["metrics"] = {k: v.text() for k, v in win.metric_values.items()}
    return state


def run_scenario(qtbot: QtBot, tmp_path: Path, animated: bool) -> dict[str, object]:
    animations.force(animated)
    # Separate settings files: computing saves the form, which would leak into the next run.
    win = MainWindow(settings_path=tmp_path / f"settings_{animated}.json")
    qtbot.addWidget(win)
    win.show()
    win.choose_input(write_template(tmp_path / f"t_{animated}.xlsx"))
    win.advanced_section.header.click()
    win.log_section.header.click()
    win.compute_button.click()
    qtbot.waitUntil(lambda: not win.is_running(), timeout=30_000)
    settle(qtbot, win)
    win.advanced_section.header.click()  # collapse again
    settle(qtbot, win)
    return window_state(win)


def test_animations_do_not_change_the_final_state(qtbot: QtBot, tmp_path: Path) -> None:
    with_animations = run_scenario(qtbot, tmp_path, animated=True)
    without = run_scenario(qtbot, tmp_path, animated=False)
    assert with_animations == without
    assert with_animations["grow"] == [1.0, 1.0, 1.0]


def test_animation_switch_combines_user_and_override() -> None:
    animations.force(None)
    animations.set_user_enabled(False)
    assert not animations.enabled()
    animations.force(True)
    assert animations.enabled()
    animations.set_user_enabled(True)


def test_user_checkbox_drives_the_switch(qtbot: QtBot) -> None:
    win = MainWindow()
    qtbot.addWidget(win)
    animations.force(None)
    win.animations_check.setChecked(False)
    assert not animations.enabled()
    win.animations_check.setChecked(True)
    assert animations.enabled() == animations.system_allows()


def test_collapsible_section(qtbot: QtBot) -> None:
    body = QLabel("body")
    section = CollapsibleSection("title", body, expanded=False)
    qtbot.addWidget(section)
    section.show()
    assert not body.isVisible()
    section.header.click()
    assert section.is_expanded() and body.isVisible()
    section.set_expanded(False, animate=False)
    assert not body.isVisible()


@pytest.mark.parametrize("animated", [False, True])
def test_toast_shows_then_hides(qtbot: QtBot, animated: bool) -> None:
    animations.force(animated)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(600, 400)
    parent.show()
    toast = Toast(parent)
    toast.show_message("บันทึกไฟล์แล้ว", duration_ms=100)
    assert toast.isVisible()
    assert toast.geometry().right() < parent.width()
    assert toast.geometry().bottom() < parent.height()
    qtbot.waitUntil(lambda: not toast.isVisible(), timeout=2_000)


# --- drag and drop, shortcuts ---------------------------------------------------------


def mime_for(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def test_dropped_path_filter(tmp_path: Path) -> None:
    xlsx = tmp_path / "a.xlsx"
    assert MainWindow.dropped_excel_path(mime_for(xlsx)) == xlsx
    assert MainWindow.dropped_excel_path(mime_for(tmp_path / "a.XLSX")) is not None
    assert MainWindow.dropped_excel_path(mime_for(tmp_path / "a.csv")) is None
    assert MainWindow.dropped_excel_path(mime_for(xlsx, tmp_path / "b.xlsx")) is None


def test_drop_sets_the_input_file(qtbot: QtBot, tmp_path: Path) -> None:
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    path = write_template(tmp_path / "dropped.xlsx")
    mime = mime_for(path)  # QDropEvent does not own the mime data: keep it alive
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    win.dropEvent(event)
    assert win.input_edit.text() == str(path)
    assert win.toast.isVisible()


def test_shortcuts(qtbot: QtBot, tmp_path: Path) -> None:
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    assert set(win.shortcuts) == {"Ctrl+O", "Ctrl+Return", "Ctrl+Enter", "Ctrl+S", "Esc"}
    win.shortcuts["Ctrl+S"].activated.emit()  # nothing to save yet: no dialog, no crash
    assert win.issues_dialog is None
    win.choose_input(write_template(tmp_path / "t.xlsx"))
    win.shortcuts["Ctrl+Return"].activated.emit()
    assert win.is_running()
    win.shortcuts["Esc"].activated.emit()
    qtbot.waitUntil(lambda: not win.is_running(), timeout=10_000)
