"""Main window: form on the left, results on the right (6.3).

Flow: fill in -> Compute (background thread) -> review results -> Save Excel.
Saving never recomputes, so a locked output file can simply be saved again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rebar_optimizer.domain.errors import (
    InputValidationError,
    Issue,
    OutputWriteError,
    SettingsError,
)
from rebar_optimizer.domain.models import CuttingPlan
from rebar_optimizer.domain.settings import (
    KERF_MAX_MM,
    KERF_MIN_MM,
    OptimizerSettings,
    StockMode,
    parse_stock_lengths_m,
)
from rebar_optimizer.i18n import th
from rebar_optimizer.i18n.th import GUI_TEXT, format_metres
from rebar_optimizer.io.reader import SUPPORTED_SUFFIXES
from rebar_optimizer.io.template import write_template
from rebar_optimizer.io.writer import WriteSource, write_plan
from rebar_optimizer.services.cancel import ProgressEvent, ProgressKind
from rebar_optimizer.services.summary import bar_counts, plan_totals, summarize_size
from rebar_optimizer.ui import animations, user_settings
from rebar_optimizer.ui.theme import Theme, current_theme
from rebar_optimizer.ui.user_settings import UserSettings
from rebar_optimizer.ui.widgets.collapsible import CollapsibleSection
from rebar_optimizer.ui.widgets.plan_preview import PlanPreview
from rebar_optimizer.ui.widgets.toast import Toast
from rebar_optimizer.ui.worker import PlanOutcome, PlanWorker, start_worker

logger = logging.getLogger(__name__)

PROGRESS_MAX = 1000
STATUS_ELAPSED_AFTER_S = 2
"""Append the seconds spent on a step once it has taken this long."""
TIME_LIMIT_RANGE_S = (1.0, 3600.0)


def check_stock_text(text: str) -> tuple[tuple[int, ...] | None, list[str]]:
    """Parse and validate the stock field: ``(lengths_mm or None, Thai errors)``."""
    try:
        lengths = parse_stock_lengths_m(text)
        OptimizerSettings(stock_lengths_mm=lengths).validate()
    except SettingsError as exc:
        return None, [th.format_issue(issue) for issue in exc.issues]
    return lengths, []


MAX_VISIBLE_SIZE_ROWS = 8
MAX_VISIBLE_COUNT_ROWS = 8
MAX_VISIBLE_WARNING_ROWS = 4


def fit_table_height(table: QTableWidget, max_rows: int) -> None:
    """Show every row up to ``max_rows``; scroll beyond that."""
    rows = max(1, min(table.rowCount(), max_rows))
    row_height = table.verticalHeader().defaultSectionSize()
    if table.rowCount():
        row_height = table.rowHeight(0)
    height = table.horizontalHeader().height() + rows * row_height + 2 * table.frameWidth()
    table.setFixedHeight(height + 2)


def fit_list_height(widget: QListWidget, max_rows: int) -> None:
    """Size a list to its items (word-wrapped ones included) up to ``max_rows`` lines."""
    widget.doItemsLayout()
    heights = [widget.sizeHintForRow(i) for i in range(widget.count())]
    line = max(heights[:1] or [widget.fontMetrics().height() + 4])
    content = sum(heights) if heights else line
    widget.setFixedHeight(min(content, line * max_rows) + 2 * widget.frameWidth() + 4)


def result_source_text(outcome: PlanOutcome, computed_at: datetime) -> str:
    """Which input and settings the shown result belongs to."""
    settings = outcome.plan.settings
    return GUI_TEXT["result_source"].format(
        name=outcome.source.path.name,
        stocks=format_stock_text(sorted(settings.stock_lengths_mm)),
        mode=th.STOCK_MODE_LABELS[settings.stock_mode],
        kerf=settings.kerf_mm,
        time=computed_at.strftime("%H:%M"),
    )


def format_stock_text(lengths_mm: Sequence[int]) -> str:
    """``(10000, 12500)`` -> ``"10, 12.5"`` (duplicates already removed by the parser)."""
    return ", ".join(f"{length / 1000:g}" for length in lengths_mm)


class IssuesDialog(QDialog):
    """Scrollable, copyable list of problems. Opened non-blocking (``open()``)."""

    def __init__(self, parent: QWidget, title: str, header: str, lines: Sequence[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        header_label = QLabel(header)
        header_label.setWordWrap(True)
        layout.addWidget(header_label)
        self.text = QPlainTextEdit("\n".join(lines))
        self.text.setReadOnly(True)
        layout.addWidget(self.text)
        buttons = QDialogButtonBox()
        copy = buttons.addButton(GUI_TEXT["copy"], QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(self._copy)
        close = buttons.addButton(GUI_TEXT["close"], QDialogButtonBox.ButtonRole.RejectRole)
        close.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings_path: Path | None = None,
        log_path: Path | None = None,
        theme: Theme | None = None,
    ) -> None:
        super().__init__()
        self._theme = theme or current_theme()
        self.previews: list[PlanPreview] = []
        self._settings_path = settings_path
        self._log_path = log_path
        self._user = user_settings.load(settings_path)
        self._outcome: PlanOutcome | None = None
        self._worker: PlanWorker | None = None
        self._thread: QThread | None = None
        self._last_saved: Path | None = None
        self.issues_dialog: IssuesDialog | None = None
        self._status_base = ""
        self._status_since = 0.0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._tick_status)

        self.setWindowTitle(GUI_TEXT["window_title"])
        self.resize(1200, 760)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._build_form())
        self.splitter.addWidget(self._build_results())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([400, 800])
        self.setCentralWidget(self.splitter)

        self.toast = Toast(self)
        self.setAcceptDrops(True)
        self._install_shortcuts()

        self._apply_user_settings()
        self._on_stock_changed(self.stock_edit.text())
        self._show_results(None)

    def _install_shortcuts(self) -> None:
        bindings = [
            ("Ctrl+O", self._shortcut_open),
            ("Ctrl+Return", self.start_computation),
            ("Ctrl+Enter", self.start_computation),
            ("Ctrl+S", self._shortcut_save),
            ("Esc", self.cancel_computation),
        ]
        self.shortcuts: dict[str, QShortcut] = {}
        for sequence, slot in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(slot)
            self.shortcuts[sequence] = shortcut

    def _shortcut_open(self) -> None:
        if not self.is_running():
            self.choose_input()

    def _shortcut_save(self) -> None:
        if self.save_button.isEnabled():
            self.save_result()

    # --- drag and drop -------------------------------------------------------------------

    @staticmethod
    def dropped_excel_path(mime: QMimeData) -> Path | None:
        """The single local .xlsx/.xlsm file being dragged, if that is what it is."""
        urls = [u for u in mime.urls() if u.isLocalFile()]
        if len(urls) != 1:
            return None
        path = Path(urls[0].toLocalFile())
        return path if path.suffix.lower() in SUPPORTED_SUFFIXES else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        if not self.is_running() and self.dropped_excel_path(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        path = self.dropped_excel_path(event.mimeData())
        if path is None or self.is_running():
            self.toast.show_message(GUI_TEXT["drop_not_xlsx"])
            event.ignore()
            return
        self.choose_input(path)
        self.toast.show_message(GUI_TEXT["file_dropped"].format(name=path.name))
        event.acceptProposedAction()

    # --- layout --------------------------------------------------------------------

    def _build_form(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 6, 12)

        input_group = QGroupBox(GUI_TEXT["input_group"])
        input_layout = QVBoxLayout(input_group)
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText(GUI_TEXT["input_placeholder"])
        self.input_edit.textChanged.connect(self._update_actions)
        input_layout.addWidget(self.input_edit)
        row = QHBoxLayout()
        self.browse_button = QPushButton(GUI_TEXT["browse"])
        self.browse_button.clicked.connect(lambda: self.choose_input())
        self.template_button = QPushButton(GUI_TEXT["make_template"])
        self.template_button.clicked.connect(lambda: self.create_template())
        row.addWidget(self.browse_button)
        row.addWidget(self.template_button)
        row.addStretch()
        input_layout.addLayout(row)
        layout.addWidget(input_group)

        stock_group = QGroupBox(GUI_TEXT["stock_group"])
        stock_layout = QVBoxLayout(stock_group)
        stock_layout.addWidget(QLabel(GUI_TEXT["stock_label"]))
        self.stock_edit = QLineEdit()
        self.stock_edit.setPlaceholderText(GUI_TEXT["stock_placeholder"])
        self.stock_edit.textChanged.connect(self._on_stock_changed)
        self.stock_edit.editingFinished.connect(self._normalize_stock_text)
        stock_layout.addWidget(self.stock_edit)
        self.stock_error = QLabel()
        self.stock_error.setObjectName("fieldError")
        self.stock_error.setWordWrap(True)
        stock_layout.addWidget(self.stock_error)
        self.mixed_radio = QRadioButton(GUI_TEXT["mode_mixed"])
        self.single_radio = QRadioButton(GUI_TEXT["mode_single"])
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mixed_radio)
        self.mode_group.addButton(self.single_radio)
        stock_layout.addWidget(self.mixed_radio)
        stock_layout.addWidget(self.single_radio)
        kerf_row = QHBoxLayout()
        self.kerf_check = QCheckBox(GUI_TEXT["kerf_check"])
        self.kerf_spin = QSpinBox()
        self.kerf_spin.setRange(KERF_MIN_MM, KERF_MAX_MM)
        self.kerf_spin.setSuffix(GUI_TEXT["kerf_suffix"])
        self.kerf_check.toggled.connect(self.kerf_spin.setEnabled)
        kerf_row.addWidget(self.kerf_check)
        kerf_row.addWidget(self.kerf_spin)
        kerf_row.addStretch()
        stock_layout.addLayout(kerf_row)
        layout.addWidget(stock_group)

        self.advanced_box = QGroupBox()
        advanced_layout = QGridLayout(self.advanced_box)
        advanced_layout.addWidget(QLabel(GUI_TEXT["time_limit_label"]), 0, 0)
        self.time_limit_spin = QDoubleSpinBox()
        self.time_limit_spin.setRange(*TIME_LIMIT_RANGE_S)
        self.time_limit_spin.setDecimals(0)
        self.time_limit_spin.setSuffix(GUI_TEXT["time_limit_suffix"])
        advanced_layout.addWidget(self.time_limit_spin, 0, 1)
        self.animations_check = QCheckBox(GUI_TEXT["animations"])
        self.animations_check.toggled.connect(animations.set_user_enabled)
        advanced_layout.addWidget(self.animations_check, 1, 0, 1, 2)
        self.advanced_section = CollapsibleSection(GUI_TEXT["advanced"], self.advanced_box)
        layout.addWidget(self.advanced_section)

        buttons = QHBoxLayout()
        self.compute_button = QPushButton(GUI_TEXT["compute"])
        self.compute_button.setObjectName("primary")
        self.compute_button.clicked.connect(self.start_computation)
        self.cancel_button = QPushButton(GUI_TEXT["cancel"])
        self.cancel_button.setObjectName("danger")
        self.cancel_button.clicked.connect(self.cancel_computation)
        self.clear_button = QPushButton(GUI_TEXT["clear"])
        self.clear_button.clicked.connect(self.clear_form)
        buttons.addWidget(self.compute_button, 1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, PROGRESS_MAX)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel(GUI_TEXT["status_ready"])
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setMinimumHeight(120)
        self.log_section = CollapsibleSection(GUI_TEXT["log_toggle"], self.log_view)
        layout.addWidget(self.log_section)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(340)
        return scroll

    def _build_results(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 12, 12, 12)

        self.empty_label = QLabel(GUI_TEXT["results_empty"])
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label, 1)

        self.unplaced_banner = QFrame()
        self.unplaced_banner.setObjectName("warningBanner")
        banner_layout = QVBoxLayout(self.unplaced_banner)
        self.unplaced_label = QLabel()
        self.unplaced_label.setWordWrap(True)
        self.unplaced_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        banner_layout.addWidget(self.unplaced_label)
        layout.addWidget(self.unplaced_banner)

        self.source_label = QLabel()
        self.source_label.setObjectName("muted")
        self.source_label.setWordWrap(True)
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.source_label)

        self.summary_card = QFrame()
        self.summary_card.setObjectName("summaryCard")
        metrics = QGridLayout(self.summary_card)
        self.metric_values: dict[str, QLabel] = {}
        keys = [
            "total_stock",
            "total_weight",
            "total_reusable",
            "total_waste",
            "utilization",
            "worst_status",
        ]
        for column, key in enumerate(keys):
            label = QLabel(GUI_TEXT[key])
            label.setObjectName("metricLabel")
            value = QLabel("—")
            value.setObjectName("metricValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            metrics.addWidget(label, 0, column)
            metrics.addWidget(value, 1, column)
            self.metric_values[key] = value
        layout.addWidget(self.summary_card)

        self.size_table = QTableWidget(0, len(th.GUI_SIZE_TABLE_HEADERS))
        self.size_table.setHorizontalHeaderLabels(list(th.GUI_SIZE_TABLE_HEADERS))
        self.size_table.verticalHeader().setVisible(False)
        self.size_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.size_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.size_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.size_table)

        counts_header = QHBoxLayout()
        self.bar_counts_title = QLabel(th.BAR_COUNT_TITLE)
        self.bar_counts_title.setObjectName("metricLabel")
        self.copy_counts_button = QPushButton(GUI_TEXT["copy"])
        self.copy_counts_button.clicked.connect(self.copy_bar_counts)
        counts_header.addWidget(self.bar_counts_title, 1)
        counts_header.addWidget(self.copy_counts_button)
        layout.addLayout(counts_header)
        self.bar_counts_list = QListWidget()
        layout.addWidget(self.bar_counts_list)

        self.preview_tabs = QTabWidget()
        self.preview_tabs.setDocumentMode(True)
        layout.addWidget(self.preview_tabs, 1)

        self.warnings_title = QLabel(GUI_TEXT["warnings_title"])
        self.warnings_title.setObjectName("metricLabel")
        layout.addWidget(self.warnings_title)
        self.warnings_list = QListWidget()
        self.warnings_list.setWordWrap(True)
        layout.addWidget(self.warnings_list)

        save_row = QHBoxLayout()
        self.save_button = QPushButton(GUI_TEXT["save"])
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(lambda: self.save_result())
        self.open_file_button = QPushButton(GUI_TEXT["open_file"])
        self.open_file_button.clicked.connect(self._open_saved_file)
        self.open_folder_button = QPushButton(GUI_TEXT["open_folder"])
        self.open_folder_button.clicked.connect(self._open_saved_folder)
        self.saved_label = QLabel()
        self.saved_label.setObjectName("muted")
        self.saved_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        save_row.addWidget(self.save_button)
        save_row.addWidget(self.open_file_button)
        save_row.addWidget(self.open_folder_button)
        save_row.addWidget(self.saved_label, 1)
        layout.addLayout(save_row)
        return panel

    # --- user settings --------------------------------------------------------------

    def _apply_user_settings(self) -> None:
        u = self._user
        self.stock_edit.setText(u.stock_text)
        (self.single_radio if u.stock_mode == StockMode.SINGLE else self.mixed_radio).setChecked(
            True
        )
        self.kerf_check.setChecked(u.kerf_enabled)
        self.kerf_spin.setValue(u.kerf_mm)
        self.kerf_spin.setEnabled(u.kerf_enabled)
        self.time_limit_spin.setValue(u.time_limit_s)
        self.animations_check.setChecked(u.animations)
        animations.set_user_enabled(u.animations)
        self.advanced_section.set_expanded(u.advanced_open, animate=False)
        if u.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(u.window_geometry.encode("ascii")))
        if u.splitter_state:
            self.splitter.restoreState(QByteArray.fromBase64(u.splitter_state.encode("ascii")))

    def collect_user_settings(self) -> UserSettings:
        u = self._user
        u.stock_text = self.stock_edit.text()
        u.stock_mode = (
            StockMode.SINGLE if self.single_radio.isChecked() else StockMode.MIXED
        ).value
        u.kerf_enabled = self.kerf_check.isChecked()
        u.kerf_mm = self.kerf_spin.value()
        u.time_limit_s = self.time_limit_spin.value()
        u.animations = self.animations_check.isChecked()
        u.advanced_open = self.advanced_section.is_expanded()
        u.window_geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        u.splitter_state = bytes(self.splitter.saveState().toBase64().data()).decode("ascii")
        return u

    def save_user_settings(self) -> None:
        user_settings.save(self.collect_user_settings(), self._settings_path)

    # --- form behaviour ----------------------------------------------------------------

    def _on_stock_changed(self, text: str) -> None:
        lengths, errors = check_stock_text(text)
        self.stock_edit.setProperty("invalid", lengths is None)
        self.stock_edit.style().unpolish(self.stock_edit)
        self.stock_edit.style().polish(self.stock_edit)
        self.stock_error.setText("\n".join(errors))
        self.stock_error.setVisible(bool(errors))
        self._update_actions()

    def _normalize_stock_text(self) -> None:
        lengths, _ = check_stock_text(self.stock_edit.text())
        if lengths is not None:
            normalized = format_stock_text(lengths)
            if normalized != self.stock_edit.text():
                self.stock_edit.setText(normalized)

    def is_running(self) -> bool:
        return self._worker is not None

    def form_is_valid(self) -> bool:
        lengths, _ = check_stock_text(self.stock_edit.text())
        return lengths is not None and bool(self.input_edit.text().strip())

    def _update_actions(self) -> None:
        running = self.is_running()
        self.compute_button.setEnabled(not running and self.form_is_valid())
        self.cancel_button.setVisible(running)
        for widget in (
            self.input_edit,
            self.browse_button,
            self.template_button,
            self.stock_edit,
            self.mixed_radio,
            self.single_radio,
            self.kerf_check,
            self.advanced_box,
            self.clear_button,
        ):
            widget.setEnabled(not running)
        self.kerf_spin.setEnabled(not running and self.kerf_check.isChecked())
        has_result = self._outcome is not None
        self.save_button.setEnabled(has_result and not running)
        self.open_file_button.setVisible(self._last_saved is not None)
        self.open_folder_button.setVisible(self._last_saved is not None)

    def current_settings(self) -> OptimizerSettings:
        lengths = parse_stock_lengths_m(self.stock_edit.text())
        return OptimizerSettings(
            stock_lengths_mm=lengths,
            stock_mode=StockMode.SINGLE if self.single_radio.isChecked() else StockMode.MIXED,
            kerf_mm=self.kerf_spin.value() if self.kerf_check.isChecked() else 0,
            time_limit_s=self.time_limit_spin.value(),
        )

    def clear_results(self) -> None:
        """Forget the shown result so it can never be saved for another input."""
        self._outcome = None
        self._last_saved = None
        self.saved_label.clear()
        self._show_results(None)

    def clear_form(self) -> None:
        """Reset the form to defaults; only ever triggered by the user."""
        self.clear_results()
        self.input_edit.clear()
        self._user = UserSettings(
            window_geometry=self._user.window_geometry,
            splitter_state=self._user.splitter_state,
            last_input_dir=self._user.last_input_dir,
            last_output_dir=self._user.last_output_dir,
        )
        self._apply_user_settings()

    # --- file choosers ------------------------------------------------------------------

    def choose_input(self, path: Path | None = None) -> None:
        if path is None:
            name, _ = QFileDialog.getOpenFileName(
                self,
                GUI_TEXT["dialog_open_title"],
                self._user.last_input_dir,
                GUI_TEXT["xlsx_filter"],
            )
            if not name:
                return
            path = Path(name)
        if str(path) != self.input_edit.text().strip():
            self.clear_results()
        self.input_edit.setText(str(path))
        self._user.last_input_dir = str(path.parent)

    def create_template(self, path: Path | None = None) -> None:
        if path is None:
            start = Path(self._user.last_input_dir or Path.home()) / GUI_TEXT["default_template"]
            name, _ = QFileDialog.getSaveFileName(
                self, GUI_TEXT["dialog_template_title"], str(start), GUI_TEXT["xlsx_filter"]
            )
            if not name:
                return
            path = Path(name)
        try:
            written = write_template(path)
        except OutputWriteError as exc:
            self._show_issues(GUI_TEXT["error_output_header"], exc.issues)
            return
        self.choose_input(written)
        self.status_label.setText(GUI_TEXT["template_saved"].format(path=written))

    # --- computation ---------------------------------------------------------------------

    def start_computation(self) -> None:
        if self.is_running() or not self.form_is_valid():
            return
        try:
            settings = self.current_settings()
            settings.validate()
        except SettingsError as exc:
            self._show_issues(GUI_TEXT["error_settings_header"], exc.issues)
            return
        path = Path(self.input_edit.text().strip())
        self.save_user_settings()
        # A new run replaces the old result even if it fails or is cancelled.
        self.clear_results()
        self.log_view.clear()
        self._log(GUI_TEXT["log_started"].format(path=path))
        animations.animate_progress(self.progress_bar, 0)
        self.status_label.setText(GUI_TEXT["status_reading"])

        worker = PlanWorker(path, settings)
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(self._on_succeeded)
        worker.input_invalid.connect(self._on_input_invalid)
        worker.cancelled.connect(self._on_cancelled)
        worker.crashed.connect(self._on_crashed)
        self._worker = worker
        self._update_actions()
        self._thread = start_worker(worker)
        self._thread.finished.connect(self._on_thread_finished)
        self._status_base = GUI_TEXT["status_reading"]
        self._status_since = time.monotonic()
        self._status_timer.start()

    def cancel_computation(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText(GUI_TEXT["status_cancelling"])
            self.cancel_button.setEnabled(False)

    def wait_for_worker(self, timeout_ms: int = 10_000) -> bool:
        """Block until the worker thread ends (used on close and in tests)."""
        thread = self._thread
        return thread is None or thread.wait(timeout_ms)

    def _on_progress(self, event: ProgressEvent) -> None:
        animations.animate_progress(self.progress_bar, round(event.fraction * PROGRESS_MAX))
        params = {"size": event.size, "index": event.index + 1, "total": event.total}
        if event.kind is ProgressKind.SIZE_STARTED:
            self._set_running_status(GUI_TEXT["status_size"].format(**params))
            self._log(GUI_TEXT["log_size_started"].format(**params))
        elif event.kind is ProgressKind.STAGE and event.stage is not None:
            stage = th.STAGE_LABELS[event.stage]
            step = th.stage_step_text(event.step, event.steps)
            self._set_running_status(
                GUI_TEXT["status_stage"].format(stage=stage, step=step, **params)
            )
            self._log(GUI_TEXT["log_stage"].format(size=event.size, stage=stage, step=step))
        else:
            self._log(GUI_TEXT["log_size_finished"].format(**params))

    def _set_running_status(self, text: str) -> None:
        """Show ``text``; the tick timer appends the seconds spent on it."""
        self._status_base = text
        self._status_since = time.monotonic()
        animations.set_text_faded(self.status_label, text)

    def _tick_status(self) -> None:
        if not self.is_running() or not self._status_base:
            return
        seconds = int(time.monotonic() - self._status_since)
        if seconds >= STATUS_ELAPSED_AFTER_S:
            self.status_label.setText(
                self._status_base + GUI_TEXT["elapsed"].format(seconds=seconds)
            )

    def _on_succeeded(self, outcome: PlanOutcome) -> None:
        self._outcome = outcome
        self._last_saved = None
        self.saved_label.clear()
        animations.animate_progress(self.progress_bar, PROGRESS_MAX)
        animations.set_text_faded(self.status_label, GUI_TEXT["status_done"])
        self._show_results(outcome)

    def _on_input_invalid(self, exc: InputValidationError | SettingsError) -> None:
        self.status_label.setText(GUI_TEXT["status_failed"])
        header = (
            GUI_TEXT["error_input_header"]
            if isinstance(exc, InputValidationError)
            else GUI_TEXT["error_settings_header"]
        )
        self._show_issues(header, exc.issues)

    def _on_cancelled(self) -> None:
        animations.animate_progress(self.progress_bar, 0)
        animations.set_text_faded(self.status_label, GUI_TEXT["status_cancelled"])

    def _on_crashed(self, message: str) -> None:
        self.status_label.setText(GUI_TEXT["status_failed"])
        self._log(message)
        text = GUI_TEXT["unexpected"].format(log_path=self._log_path or "-")
        self._open_dialog(IssuesDialog(self, GUI_TEXT["unexpected_title"], text, [message]))

    def _on_thread_finished(self) -> None:
        # Only now is it safe to drop the last references to the thread and worker.
        self._worker = None
        self._thread = None
        self._status_timer.stop()
        self._status_base = ""
        self.cancel_button.setEnabled(True)
        self._update_actions()

    # --- results -----------------------------------------------------------------------

    def _show_results(self, outcome: PlanOutcome | None) -> None:
        has = outcome is not None
        self.empty_label.setVisible(not has)
        result_widgets = (
            self.source_label,
            self.summary_card,
            self.size_table,
            self.bar_counts_title,
            self.copy_counts_button,
            self.bar_counts_list,
            self.preview_tabs,
            self.warnings_title,
            self.warnings_list,
        )
        for widget in result_widgets:
            if has:
                animations.fade_in(widget)
            else:
                widget.setVisible(False)
        if outcome is not None and outcome.plan.unplaced:
            animations.fade_in(self.unplaced_banner)
        else:
            self.unplaced_banner.setVisible(False)
        self._update_actions()
        if outcome is None:
            return
        plan = outcome.plan
        self.source_label.setText(result_source_text(outcome, datetime.now()))
        totals = plan_totals(plan)
        values = self.metric_values
        stock_text = GUI_TEXT["metres"].format(value=format_metres(totals.stock_mm))
        if totals.inventory_mm:
            stock_text += "\n" + GUI_TEXT["from_inventory"].format(
                value=format_metres(totals.inventory_mm)
            )
        values["total_stock"].setText(stock_text)
        values["total_weight"].setText(
            GUI_TEXT["kg"].format(value=totals.weight_kg)
            if totals.weight_kg is not None
            else GUI_TEXT["weight_unknown"]
        )
        values["total_reusable"].setText(
            GUI_TEXT["metres"].format(value=format_metres(totals.reusable_mm))
        )
        values["total_waste"].setText(
            GUI_TEXT["metres"].format(value=format_metres(totals.waste_mm))
        )
        values["utilization"].setText(GUI_TEXT["percent"].format(value=totals.utilization * 100))
        worst = plan.worst_status
        values["worst_status"].setText(str(worst) if worst else "—")
        values["worst_status"].setToolTip(th.STATUS_LABELS[worst] if worst else "")

        if plan.unplaced:
            items = [
                GUI_TEXT["unplaced_item"].format(
                    size=u.size, no=u.no, length_m=format_metres(u.length_mm), quantity=u.quantity
                )
                for u in plan.unplaced
            ]
            count = sum(u.quantity for u in plan.unplaced)
            self.unplaced_label.setText(
                GUI_TEXT["unplaced_banner"].format(count=count) + "\n" + "\n".join(items)
            )

        self.size_table.setRowCount(len(plan.sizes))
        for row, size_plan in enumerate(plan.sizes):
            summary = summarize_size(size_plan)
            gap = size_plan.gap
            cells = [
                size_plan.size,
                str(summary.bar_count),
                format_metres(summary.stock_mm),
                GUI_TEXT["percent"].format(value=summary.utilization * 100)
                if summary.stock_mm
                else "—",
                str(size_plan.status) if size_plan.groups else "—",
                GUI_TEXT["percent"].format(value=gap * 100)
                if gap is not None and size_plan.groups
                else "—",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if column == 4 and size_plan.groups:
                    item.setToolTip(th.STATUS_LABELS[size_plan.status])
                self.size_table.setItem(row, column, item)
        fit_table_height(self.size_table, MAX_VISIBLE_SIZE_ROWS)

        self.bar_counts_list.clear()
        self.bar_counts_list.addItems(
            [
                th.format_bar_count(c.size, c.count, c.stock_length_mm, c.inventory)
                for c in bar_counts(plan)
            ]
            or [th.NONE_TEXT]
        )
        fit_list_height(self.bar_counts_list, MAX_VISIBLE_COUNT_ROWS)

        self._show_previews(plan)

        self.warnings_list.clear()
        messages = [th.format_issue(i) for i in (*outcome.source.warnings, *plan.issues)]
        self.warnings_list.addItems(messages or [th.NONE_TEXT])
        fit_list_height(self.warnings_list, MAX_VISIBLE_WARNING_ROWS)

    def copy_bar_counts(self) -> None:
        """Copy the per-size, per-length lines, e.g. to paste into an order."""
        lines = [self.bar_counts_list.item(i).text() for i in range(self.bar_counts_list.count())]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.toast.show_message(GUI_TEXT["copied"])

    def _show_previews(self, plan: CuttingPlan) -> None:
        self.preview_tabs.clear()
        self.previews = []
        stocks = [g.bar.stock_length_mm for sp in plan.sizes for g in sp.groups]
        longest = max(stocks, default=1)
        for size_plan in plan.sizes:
            preview = PlanPreview(self._theme)
            preview.set_plan(size_plan, max_stock_mm=longest)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(preview)
            self.preview_tabs.addTab(scroll, size_plan.size)
            self.previews.append(preview)

    # --- saving -------------------------------------------------------------------------

    def default_output_path(self) -> Path:
        folder = Path(self._user.last_output_dir or self._user.last_input_dir or Path.home())
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        return folder / GUI_TEXT["default_output"].format(stamp=stamp)

    def save_result(self, path: Path | None = None) -> bool:
        """Write the current result; returns ``True`` on success. Never recomputes."""
        if self._outcome is None:
            return False
        if path is None:
            name, _ = QFileDialog.getSaveFileName(
                self,
                GUI_TEXT["dialog_save_title"],
                str(self.default_output_path()),
                GUI_TEXT["xlsx_filter"],
            )
            if not name:
                return False
            path = Path(name)
        source = self._outcome.source
        try:
            written = write_plan(
                path,
                self._outcome.plan,
                WriteSource(
                    source.pieces,
                    source.table,
                    source.path,
                    source.warnings,
                    inventory=source.inventory,
                    inventory_sheet=source.inventory_sheet,
                ),
            )
        except OutputWriteError as exc:
            self._show_issues(GUI_TEXT["error_output_header"], exc.issues)
            return False
        self._last_saved = written
        self._user.last_output_dir = str(written.parent)
        self.saved_label.setText(GUI_TEXT["saved"].format(path=written))
        self.toast.show_message(GUI_TEXT["saved"].format(path=written.name))
        self.saved_label.setToolTip(str(written))
        self._update_actions()
        return True

    def _open_saved_file(self) -> None:
        if self._last_saved is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_saved)))

    def _open_saved_folder(self) -> None:
        if self._last_saved is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_saved.parent)))

    # --- helpers --------------------------------------------------------------------------

    def _log(self, line: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {line}")

    def _show_issues(self, header: str, issues: Sequence[Issue]) -> None:
        self._open_dialog(
            IssuesDialog(self, GUI_TEXT["error_title"], header, th.format_issues(issues))
        )

    def _open_dialog(self, dialog: IssuesDialog) -> None:
        if self.issues_dialog is not None:
            self.issues_dialog.close()
        self.issues_dialog = dialog
        dialog.open()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._worker is not None:
            self._worker.cancel()
            self.wait_for_worker()
        self.save_user_settings()
        super().closeEvent(event)
