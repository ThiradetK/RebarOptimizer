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
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from rebar_optimizer.domain.errors import Cancelled, InputValidationError, SettingsError
from rebar_optimizer.domain.models import CuttingPlan
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.io.reader import ReadResult, read_pieces
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import CancelToken, ProgressEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanOutcome:
    plan: CuttingPlan
    source: ReadResult


class PlanWorker(QObject):
    progress = Signal(object)
    """``ProgressEvent``."""
    succeeded = Signal(object)
    """``PlanOutcome``."""
    input_invalid = Signal(object)
    """``InputValidationError | SettingsError``."""
    cancelled = Signal()
    crashed = Signal(str)
    """Short English description; details are in the log."""
    finished = Signal()

    def __init__(self, input_path: Path, settings: OptimizerSettings) -> None:
        super().__init__()
        self._input_path = input_path
        self._settings = settings
        self._token = CancelToken()

    def cancel(self) -> None:
        """Thread-safe: may be called from the GUI thread while ``run`` executes."""
        self._token.cancel()

    @Slot()
    def run(self) -> None:
        try:
            self._token.raise_if_cancelled()
            source = read_pieces(self._input_path)
            plan = planner.plan(
                source.pieces, self._settings, on_progress=self._emit_progress, cancel=self._token
            )
            self.succeeded.emit(PlanOutcome(plan=plan, source=source))
        except (InputValidationError, SettingsError) as exc:
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


def start_worker(worker: PlanWorker) -> QThread:
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
