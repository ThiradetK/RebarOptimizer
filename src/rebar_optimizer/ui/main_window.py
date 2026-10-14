"""Main window: form on the left, results on the right (6.3).

Flow: fill in -> Compute (background thread) -> review results -> Save Excel.
Saving never recomputes, so a locked output file can simply be saved again.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QThread, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from rebar_optimizer.domain.errors import (
    InputValidationError,
    Issue,
    OutputWriteError,
    SettingsError,
)
from rebar_optimizer.domain.settings import (
    KERF_MAX_MM,
    KERF_MIN_MM,
    OptimizerSettings,
    StockMode,
    parse_stock_lengths_m,
)
from rebar_optimizer.i18n import th
from rebar_optimizer.i18n.th import GUI_TEXT, format_metres
from rebar_optimizer.io.template import write_template
from rebar_optimizer.io.writer import WriteSource, write_plan
from rebar_optimizer.services.cancel import ProgressEvent, ProgressKind
from rebar_optimizer.services.summary import plan_totals, summarize_size
from rebar_optimizer.ui import user_settings
from rebar_optimizer.ui.user_settings import UserSettings
from rebar_optimizer.ui.worker import PlanOutcome, PlanWorker, start_worker

logger = logging.getLogger(__name__)

PROGRESS_MAX = 1000
TIME_LIMIT_RANGE_S = (1.0, 3600.0)


def check_stock_text(text: str) -> tuple[tuple[int, ...] | None, list[str]]:
    """Parse and validate the stock field: ``(lengths_mm or None, Thai errors)``."""
    try:
        lengths = parse_stock_lengths_m(text)
        OptimizerSettings(stock_lengths_mm=lengths).validate()
    except SettingsError as exc:
        return None, [th.format_issue(issue) for issue in exc.issues]
    return lengths, []


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
    def __init__(self, settings_path: Path | None = None, log_path: Path | None = None) -> None:
        super().__init__()
        self._settings_path = settings_path
        self._log_path = log_path
        self._user = user_settings.load(settings_path)
        self._outcome: PlanOutcome | None = None
        self._worker: PlanWorker | None = None
        self._thread: QThread | None = None
        self._last_saved: Path | None = None
        self.issues_dialog: IssuesDialog | None = None

        self.setWindowTitle(GUI_TEXT["window_title"])
        self.resize(1200, 760)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._build_form())
        self.splitter.addWidget(self._build_results())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([400, 800])
        self.setCentralWidget(self.splitter)

        self._apply_user_settings()
        self._on_stock_changed(self.stock_edit.text())
        self._show_results(None)

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

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText(GUI_TEXT["advanced"])
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.toggled.connect(self._set_advanced_open)
        layout.addWidget(self.advanced_toggle)
        self.advanced_box = QGroupBox()
        advanced_layout = QGridLayout(self.advanced_box)
        advanced_layout.addWidget(QLabel(GUI_TEXT["time_limit_label"]), 0, 0)
        self.time_limit_spin = QDoubleSpinBox()
        self.time_limit_spin.setRange(*TIME_LIMIT_RANGE_S)
        self.time_limit_spin.setDecimals(0)
        self.time_limit_spin.setSuffix(GUI_TEXT["time_limit_suffix"])
        advanced_layout.addWidget(self.time_limit_spin, 0, 1)
        self.animations_check = QCheckBox(GUI_TEXT["animations"])
        advanced_layout.addWidget(self.animations_check, 1, 0, 1, 2)
        layout.addWidget(self.advanced_box)

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
        self.log_toggle = QToolButton()
        self.log_toggle.setText(GUI_TEXT["log_toggle"])
        self.log_toggle.setCheckable(True)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setVisible(False)
        self.log_toggle.toggled.connect(self.log_view.setVisible)
        layout.addWidget(self.log_toggle)
        layout.addWidget(self.log_view, 1)
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
        layout.addWidget(self.size_table, 1)

        self.warnings_title = QLabel(GUI_TEXT["warnings_title"])
        self.warnings_title.setObjectName("metricLabel")
        layout.addWidget(self.warnings_title)
        self.warnings_list = QListWidget()
        self.warnings_list.setWordWrap(True)
        self.warnings_list.setMaximumHeight(96)
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
        self.advanced_toggle.setChecked(u.advanced_open)
        self._set_advanced_open(u.advanced_open)
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
        u.advanced_open = self.advanced_toggle.isChecked()
        u.window_geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        u.splitter_state = bytes(self.splitter.saveState().toBase64().data()).decode("ascii")
        return u

    def save_user_settings(self) -> None:
        user_settings.save(self.collect_user_settings(), self._settings_path)

    # --- form behaviour ----------------------------------------------------------------

    def _set_advanced_open(self, open_: bool) -> None:
        self.advanced_box.setVisible(open_)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow
        )

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

    def clear_form(self) -> None:
        """Reset the form to defaults; only ever triggered by the user."""
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
        self.log_view.clear()
        self._log(GUI_TEXT["log_started"].format(path=path))
        self.progress_bar.setValue(0)
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
        self.progress_bar.setValue(round(event.fraction * PROGRESS_MAX))
        params = {"size": event.size, "index": event.index + 1, "total": event.total}
        if event.kind is ProgressKind.SIZE_STARTED:
            self.status_label.setText(GUI_TEXT["status_size"].format(**params))
            self._log(GUI_TEXT["log_size_started"].format(**params))
        else:
            self._log(GUI_TEXT["log_size_finished"].format(**params))

    def _on_succeeded(self, outcome: PlanOutcome) -> None:
        self._outcome = outcome
        self._last_saved = None
        self.saved_label.clear()
        self.progress_bar.setValue(PROGRESS_MAX)
        self.status_label.setText(GUI_TEXT["status_done"])
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
        self.progress_bar.setValue(0)
        self.status_label.setText(GUI_TEXT["status_cancelled"])

    def _on_crashed(self, message: str) -> None:
        self.status_label.setText(GUI_TEXT["status_failed"])
        self._log(message)
        text = GUI_TEXT["unexpected"].format(log_path=self._log_path or "-")
        self._open_dialog(IssuesDialog(self, GUI_TEXT["unexpected_title"], text, [message]))

    def _on_thread_finished(self) -> None:
        # Only now is it safe to drop the last references to the thread and worker.
        self._worker = None
        self._thread = None
        self.cancel_button.setEnabled(True)
        self._update_actions()

    # --- results -----------------------------------------------------------------------

    def _show_results(self, outcome: PlanOutcome | None) -> None:
        has = outcome is not None
        self.empty_label.setVisible(not has)
        for widget in (self.summary_card, self.size_table, self.warnings_title, self.warnings_list):
            widget.setVisible(has)
        self.unplaced_banner.setVisible(bool(outcome and outcome.plan.unplaced))
        self._update_actions()
        if outcome is None:
            return
        plan = outcome.plan
        totals = plan_totals(plan)
        values = self.metric_values
        values["total_stock"].setText(
            GUI_TEXT["metres"].format(value=format_metres(totals.stock_mm))
        )
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

        self.warnings_list.clear()
        messages = [th.format_issue(i) for i in (*outcome.source.warnings, *plan.issues)]
        self.warnings_list.addItems(messages or [th.NONE_TEXT])

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
                WriteSource(source.pieces, source.table, source.path, source.warnings),
            )
        except OutputWriteError as exc:
            self._show_issues(GUI_TEXT["error_output_header"], exc.issues)
            return False
        self._last_saved = written
        self._user.last_output_dir = str(written.parent)
        self.saved_label.setText(GUI_TEXT["saved"].format(path=written))
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
