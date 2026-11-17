"""Background computation (D9, 6.3).

``PlanWorker`` lives on a ``QThread``. It reads the input and runs the planner,
forwarding core progress callbacks as Qt signals. It never touches widgets; the
main window reacts to its signals on the GUI thread.

Signal order for one run: ``progress``* then exactly one of ``succeeded`` /
``input_invalid`` / ``cancelled`` / ``crashed``, then ``finished``; after that
the thread's own ``finished`` signal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from rebar_optimizer.domain.errors import (
    Cancelled,
    InputValidationError,
    OutputWriteError,
    PlanningFailed,
    SettingsError,
)
from rebar_optimizer.domain.models import CuttingPlan
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.io.reader import ReadResult, read_pieces
from rebar_optimizer.io.writer import WriteSource, write_plan
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import CancelToken, ProgressEvent
from rebar_optimizer.services.summary import plan_totals
from rebar_optimizer.ui.widgets.plan_preview import PreviewData, prepare_preview

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanOutcome:
    plan: CuttingPlan
    source: ReadResult
    previews: tuple[PreviewData, ...] = ()
    """One per size, prepared on the worker thread (empty: prepare on demand)."""


class PlanWorker(QObject):
    progress = Signal(object)
    """``ProgressEvent``."""
    succeeded = Signal(object)
    """``PlanOutcome``."""
    input_invalid = Signal(object)
    """``InputValidationError | SettingsError | PlanningFailed``: nothing to show,
    the issues say why."""
    cancelled = Signal()
    crashed = Signal(str)
    """Short English description; details are in the log."""
    finished = Signal()

    def __init__(
        self, input_path: Path, settings: OptimizerSettings, source: ReadResult | None = None
    ) -> None:
        """``source``: the already-read input (skips reading ``input_path`` again)."""
        super().__init__()
        self._input_path = input_path
        self._settings = settings
        self._source = source
        self._token = CancelToken()

    def cancel(self) -> None:
        """Thread-safe: may be called from the GUI thread while ``run`` executes."""
        self._token.cancel()

    @Slot()
    def run(self) -> None:
        try:
            self._token.raise_if_cancelled()
            source = self._source if self._source is not None else read_pieces(self._input_path)
            plan = planner.plan(
                source.pieces,
                self._settings,
                on_progress=self._emit_progress,
                cancel=self._token,
                inventory=source.inventory,
            )
            # Heavy result preparation happens here, not on the GUI thread: with
            # ~90,000 bar patterns it took several seconds and froze the window.
            previews = tuple(prepare_preview(size_plan) for size_plan in plan.sizes)
            plan_totals(plan)  # fills the per-size summary cache used by the window
            self.succeeded.emit(PlanOutcome(plan=plan, source=source, previews=previews))
        except (InputValidationError, SettingsError, PlanningFailed) as exc:
            self.input_invalid.emit(exc)
        except Cancelled:
            logger.info("computation cancelled")
            self.cancelled.emit()
        except Exception as exc:
            logger.exception("computation failed")
            self.crashed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()

    def _emit_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)


class ReadWorker(QObject):
    """Reads and validates the input off the GUI thread (100,000 rows take seconds),
    so the window can check it before planning starts.

    Signal order: exactly one of ``read`` / ``input_invalid`` / ``cancelled`` /
    ``crashed``, then ``finished``.
    """

    read = Signal(object)
    """``ReadResult``."""
    input_invalid = Signal(object)
    cancelled = Signal()
    crashed = Signal(str)
    finished = Signal()

    def __init__(self, input_path: Path) -> None:
        super().__init__()
        self._input_path = input_path
        self._token = CancelToken()

    def cancel(self) -> None:
        self._token.cancel()

    @Slot()
    def run(self) -> None:
        try:
            self._token.raise_if_cancelled()
            source = read_pieces(self._input_path)
            self._token.raise_if_cancelled()
            self.read.emit(source)
        except InputValidationError as exc:
            self.input_invalid.emit(exc)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:
            logger.exception("reading failed")
            self.crashed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class SaveWorker(QObject):
    """Writes an output file (Excel or PDF) off the GUI thread: a 100,000-row
    result takes minutes and would otherwise freeze the window.

    Signal order: exactly one of ``saved`` / ``failed`` / ``crashed``, then ``finished``.
    """

    saved = Signal(object)
    """``Path`` written."""
    failed = Signal(object)
    """``OutputWriteError`` (e.g. the file is open in Excel or a PDF viewer)."""
    crashed = Signal(str)
    finished = Signal()

    def __init__(self, write: Callable[[], Path]) -> None:
        """``write`` runs on the worker thread and returns the path written."""
        super().__init__()
        self._write = write

    @classmethod
    def excel(cls, path: Path, plan: CuttingPlan, source: WriteSource) -> SaveWorker:
        return cls(lambda: write_plan(path, plan, source))

    @Slot()
    def run(self) -> None:
        try:
            self.saved.emit(self._write())
        except OutputWriteError as exc:
            self.failed.emit(exc)
        except Exception as exc:
            logger.exception("saving failed")
            self.crashed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


def start_worker(worker: PlanWorker | ReadWorker | SaveWorker) -> QThread:
    """Move ``worker`` to a new thread and start it; the thread quits when the
    worker reports ``finished``.

    The caller must keep Python references to both the worker and the returned
    thread until the *thread's* ``finished`` signal: destroying a ``QThread``
    that is still winding down aborts the whole process.
    """
    thread = QThread()
    thread.setObjectName("plan-worker")
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    thread.start()
    return thread
