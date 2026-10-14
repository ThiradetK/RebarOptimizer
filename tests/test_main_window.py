from __future__ import annotations

import os
from pathlib import Path

import openpyxl
import pytest
from pytestqt.qtbot import QtBot

from benchmarks.generate import PRESETS, make_pieces, write_xlsx
from rebar_optimizer.domain.settings import StockMode
from rebar_optimizer.i18n.th import GUI_TEXT, SHEET_NAMES
from rebar_optimizer.io.template import write_template
from rebar_optimizer.ui.main_window import MainWindow, check_stock_text, format_stock_text
from tests.conftest import MakeXlsx

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot: QtBot) -> MainWindow:
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    return win


def compute(qtbot: QtBot, win: MainWindow, timeout_ms: int = 30_000) -> None:
    assert win.compute_button.isEnabled()
    win.compute_button.click()
    qtbot.waitUntil(lambda: not win.is_running(), timeout=timeout_ms)


# --- stock field ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lengths", "error_fragment"),
    [
        ("10, 12", (10000, 12000), None),
        ("12,10,12", (12000, 10000), None),
        ("10.5", (10500,), None),
        ("", None, "อย่างน้อย 1 ค่า"),
        ("10, abc", None, '"abc" ไม่ใช่ตัวเลข'),
        ("0", None, "ต้องมากกว่า 0"),
    ],
)
def test_check_stock_text(
    text: str, lengths: tuple[int, ...] | None, error_fragment: str | None
) -> None:
    parsed, errors = check_stock_text(text)
    assert parsed == lengths
    if error_fragment is None:
        assert errors == []
    else:
        assert any(error_fragment in e for e in errors)


def test_format_stock_text() -> None:
    assert format_stock_text((12000, 10000, 10500)) == "12, 10, 10.5"


def test_live_validation_marks_field_and_blocks_compute(window: MainWindow, tmp_path: Path) -> None:
    window.choose_input(write_template(tmp_path / "t.xlsx"))
    window.stock_edit.setText("10, x")
    assert window.stock_edit.property("invalid") is True
    assert window.stock_error.isVisible()
    assert '"x"' in window.stock_error.text()
    assert not window.compute_button.isEnabled()
    window.stock_edit.setText("10, 12")
    assert window.stock_edit.property("invalid") is False
    assert not window.stock_error.isVisible()
    assert window.compute_button.isEnabled()


def test_duplicates_removed_when_editing_finishes(window: MainWindow) -> None:
    window.stock_edit.setText("12,10,12")
    window.stock_edit.editingFinished.emit()
    assert window.stock_edit.text() == "12, 10"


def test_compute_needs_a_file(window: MainWindow) -> None:
    window.stock_edit.setText("10, 12")
    assert not window.compute_button.isEnabled()


def test_kerf_spin_follows_checkbox(window: MainWindow) -> None:
    window.kerf_check.setChecked(False)
    assert not window.kerf_spin.isEnabled()
    window.kerf_check.setChecked(True)
    assert window.kerf_spin.isEnabled()
    assert window.kerf_spin.value() == 3


def test_current_settings_mapping(window: MainWindow) -> None:
    window.stock_edit.setText("12, 10")
    window.single_radio.setChecked(True)
    window.kerf_spin.setValue(5)
    window.kerf_check.setChecked(False)
    window.time_limit_spin.setValue(12)
    settings = window.current_settings()
    assert settings.stock_lengths_mm == (12000, 10000)
    assert settings.stock_mode is StockMode.SINGLE
    assert settings.kerf_mm == 0  # unticked -> 0 through the same code path (D3)
    assert settings.time_limit_s == 12
    window.kerf_check.setChecked(True)
    assert window.current_settings().kerf_mm == 5


# --- computing ------------------------------------------------------------------------


def test_successful_run_shows_results_and_saves(
    qtbot: QtBot, window: MainWindow, tmp_path: Path
) -> None:
    window.choose_input(write_template(tmp_path / "t.xlsx"))
    window.stock_edit.setText("10, 12")
    compute(qtbot, window)
    assert window.status_label.text() == GUI_TEXT["status_done"]
    assert window.size_table.rowCount() == 3
    assert window.metric_values["total_stock"].text().endswith("ม.")
    assert window.metric_values["total_weight"].text().endswith("kg")
    assert not window.unplaced_banner.isVisible()
    assert window.save_button.isEnabled()
    assert window.progress_bar.value() == window.progress_bar.maximum()

    out = tmp_path / "plan.xlsx"
    assert window.save_result(out)
    assert out.is_file()
    assert openpyxl.load_workbook(out).sheetnames == list(SHEET_NAMES.values())
    assert window.open_file_button.isVisible()


def test_unplaced_banner(qtbot: QtBot, window: MainWindow, make_xlsx: MakeXlsx) -> None:
    window.choose_input(make_xlsx([["DB12", 1, 13.0, 2], ["DB12", 2, 3.0, 1]]))
    window.stock_edit.setText("12")
    compute(qtbot, window)
    assert window.unplaced_banner.isVisible()
    assert "No 1" in window.unplaced_label.text()
    assert "2 ชิ้น" in window.unplaced_label.text()


def test_reader_error_keeps_every_field(
    qtbot: QtBot, window: MainWindow, make_xlsx: MakeXlsx
) -> None:
    path = make_xlsx([["DB12", 1, "x", 1], ["DB12", 2, 1.0, 0]])
    window.choose_input(path)
    window.stock_edit.setText("9, 12")
    window.single_radio.setChecked(True)
    window.kerf_check.setChecked(True)
    window.kerf_spin.setValue(7)
    compute(qtbot, window)

    dialog = window.issues_dialog
    assert dialog is not None and dialog.isVisible()
    text = dialog.text.toPlainText()
    assert "แถว 2:" in text and "แถว 3:" in text
    assert window.input_edit.text() == str(path)
    assert window.stock_edit.text() == "9, 12"
    assert window.single_radio.isChecked()
    assert window.kerf_check.isChecked() and window.kerf_spin.value() == 7
    assert window.compute_button.isEnabled()


def test_unexpected_error_shows_log_message(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("rebar_optimizer.services.planner.plan", boom)
    window.choose_input(write_template(tmp_path / "t.xlsx"))
    compute(qtbot, window)
    assert window.issues_dialog is not None
    assert "RuntimeError: boom" in window.issues_dialog.text.toPlainText()
    assert window.status_label.text() == GUI_TEXT["status_failed"]


def test_cancel(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> None:
    path = tmp_path / "hard.xlsx"
    write_xlsx([p for p in make_pieces(PRESETS["medium"], seed=2) if p.size == "RB9"], path)
    window.choose_input(path)
    window.time_limit_spin.setValue(60)
    window.compute_button.click()
    assert window.is_running()
    assert window.cancel_button.isVisible()
    assert not window.stock_edit.isEnabled()  # form locked while computing
    qtbot.wait(1500)
    window.cancel_button.click()
    qtbot.waitUntil(lambda: not window.is_running(), timeout=5_000)
    assert window.status_label.text() == GUI_TEXT["status_cancelled"]
    assert not window.save_button.isEnabled()
    assert window.stock_edit.isEnabled()


# --- saving and remembering ------------------------------------------------------------


def test_locked_output_can_be_saved_again_without_recomputing(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window.choose_input(write_template(tmp_path / "t.xlsx"))
    compute(qtbot, window)
    plan_before = window._outcome

    real_replace = os.replace

    def locked(*_: object) -> None:
        raise PermissionError("open in Excel")

    monkeypatch.setattr(os, "replace", locked)
    out = tmp_path / "plan.xlsx"
    assert not window.save_result(out)
    assert window.issues_dialog is not None
    assert "กรุณาปิดไฟล์ plan.xlsx" in window.issues_dialog.text.toPlainText()

    monkeypatch.setattr(os, "replace", real_replace)
    assert window.save_result(out)
    assert window._outcome is plan_before
    assert out.is_file()


def test_last_values_are_remembered(qtbot: QtBot, tmp_path: Path) -> None:
    first = MainWindow()
    qtbot.addWidget(first)
    first.stock_edit.setText("6, 9, 12")
    first.single_radio.setChecked(True)
    first.kerf_check.setChecked(True)
    first.kerf_spin.setValue(4)
    first.time_limit_spin.setValue(20)
    first.animations_check.setChecked(False)
    first.close()

    second = MainWindow()
    qtbot.addWidget(second)
    assert second.stock_edit.text() == "6, 9, 12"
    assert second.single_radio.isChecked()
    assert second.kerf_check.isChecked() and second.kerf_spin.value() == 4
    assert second.time_limit_spin.value() == 20
    assert not second.animations_check.isChecked()


def test_clear_only_on_request(window: MainWindow, tmp_path: Path) -> None:
    window.choose_input(write_template(tmp_path / "t.xlsx"))
    window.stock_edit.setText("6, 9")
    window.clear_button.click()
    assert window.input_edit.text() == ""
    assert window.stock_edit.text() == "10, 12"
    assert window.mixed_radio.isChecked()
    assert not window.kerf_check.isChecked()


def test_template_button_fills_the_input(window: MainWindow, tmp_path: Path) -> None:
    target = tmp_path / "template.xlsx"
    window.create_template(target)
    assert target.is_file()
    assert window.input_edit.text() == str(target)
