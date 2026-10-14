from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from pytestqt.qtbot import QtBot

from benchmarks.generate import PRESETS, make_pieces, write_xlsx
from rebar_optimizer.domain.errors import InputValidationError, SettingsError
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.io.template import write_template
from rebar_optimizer.ui.worker import PlanOutcome, PlanWorker, start_worker
from tests.conftest import MakeXlsx

pytestmark = pytest.mark.gui


def run_worker(
    qtbot: QtBot, worker: PlanWorker, timeout_ms: int = 30_000, cancel_after_ms: int | None = None
) -> list[str]:
    """Run ``worker`` to completion; return the signal names in emission order.

    The lambdas run on the worker thread (plain callables have no thread
    affinity), which is fine for appending to a list.
    """
    events: list[str] = []
    worker.progress.connect(lambda e: events.append(f"progress:{e.kind}:{e.size}"))
    worker.succeeded.connect(lambda _: events.append("succeeded"))
    worker.input_invalid.connect(lambda _: events.append("input_invalid"))
    worker.cancelled.connect(lambda: events.append("cancelled"))
    worker.crashed.connect(lambda _: events.append("crashed"))
    worker.finished.connect(lambda: events.append("finished"))
    thread = start_worker(worker)
    if cancel_after_ms is not None:
        # A bound QObject method would be delivered on the busy worker thread; a lambda
        # runs here on the GUI thread, like the real Cancel button.
        QTimer.singleShot(cancel_after_ms, lambda: worker.cancel())  # noqa: PLW0108 - see above
    with qtbot.waitSignal(thread.finished, timeout=timeout_ms):
        pass
    return events


def test_signals_in_order(qtbot: QtBot, tmp_path: Path) -> None:
    path = write_template(tmp_path / "t.xlsx")
    worker = PlanWorker(path, OptimizerSettings(stock_lengths_mm=(10000, 12000)))
    outcomes: list[PlanOutcome] = []
    worker.succeeded.connect(outcomes.append)
    events = run_worker(qtbot, worker)
    assert events == [
        "progress:size_started:DB12",
        "progress:size_finished:DB12",
        "progress:size_started:DB16",
        "progress:size_finished:DB16",
        "progress:size_started:RB9",
        "progress:size_finished:RB9",
        "succeeded",
        "finished",
    ]
    assert [s.size for s in outcomes[0].plan.sizes] == ["DB12", "DB16", "RB9"]
    assert outcomes[0].source.path == path


def test_input_errors_become_a_signal(qtbot: QtBot, make_xlsx: MakeXlsx) -> None:
    worker = PlanWorker(
        make_xlsx([["DB12", 1, "x", 1]]), OptimizerSettings(stock_lengths_mm=(10000,))
    )
    errors: list[object] = []
    worker.input_invalid.connect(errors.append)
    assert run_worker(qtbot, worker) == ["input_invalid", "finished"]
    assert isinstance(errors[0], InputValidationError)


def test_settings_errors_become_a_signal(qtbot: QtBot, tmp_path: Path) -> None:
    path = write_template(tmp_path / "t.xlsx")
    worker = PlanWorker(path, OptimizerSettings(stock_lengths_mm=(10000,), kerf_mm=99))
    errors: list[object] = []
    worker.input_invalid.connect(errors.append)
    assert run_worker(qtbot, worker) == ["input_invalid", "finished"]
    assert isinstance(errors[0], SettingsError)


def test_exception_becomes_crashed_not_a_crash(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("rebar_optimizer.services.planner.plan", boom)
    worker = PlanWorker(
        write_template(tmp_path / "t.xlsx"), OptimizerSettings(stock_lengths_mm=(10000,))
    )
    messages: list[str] = []
    worker.crashed.connect(messages.append)
    assert run_worker(qtbot, worker) == ["crashed", "finished"]
    assert messages == ["RuntimeError: boom"]


def test_cancel_while_solving(qtbot: QtBot, tmp_path: Path) -> None:
    path = tmp_path / "hard.xlsx"
    write_xlsx([p for p in make_pieces(PRESETS["medium"], seed=2) if p.size == "RB9"], path)
    worker = PlanWorker(path, OptimizerSettings(stock_lengths_mm=(10000, 12000), time_limit_s=60))
    # This size takes ~6 s; after 1.5 s CP-SAT is busy searching.
    events = run_worker(qtbot, worker, timeout_ms=10_000, cancel_after_ms=1500)
    assert events[-2:] == ["cancelled", "finished"]


def test_cancel_before_start(qtbot: QtBot, tmp_path: Path) -> None:
    worker = PlanWorker(
        write_template(tmp_path / "t.xlsx"), OptimizerSettings(stock_lengths_mm=(10000,))
    )
    worker.cancel()
    assert run_worker(qtbot, worker) == ["cancelled", "finished"]
