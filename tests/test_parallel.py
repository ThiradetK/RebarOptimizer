from __future__ import annotations

import os
import threading
import time

import pytest

from benchmarks.generate import PRESETS, make_pieces
from rebar_optimizer.domain.errors import Cancelled, IssueCode, PlanningFailed
from rebar_optimizer.domain.models import CuttingPlan, InventoryStock, Piece
from rebar_optimizer.domain.settings import (
    OptimizerSettings,
    PerformanceMode,
    StockMode,
    plan_resources,
)
from rebar_optimizer.i18n import th
from rebar_optimizer.services import parallel, planner
from rebar_optimizer.services.cancel import (
    CancelToken,
    ProgressEvent,
    ProgressKind,
    StageCallback,
)
from rebar_optimizer.services.parallel import SizeResult, SizeTask, solve_task
from rebar_optimizer.services.summary import summarize_size


def piece(size: str, no: str, length_mm: int, quantity: int) -> Piece:
    return Piece(size, no, length_mm, quantity, (2,))


PIECES = [
    piece("A", "1", 3300, 7),
    piece("A", "2", 2100, 5),
    piece("B", "3", 5750, 4),
    piece("B", "4", 1200, 9),
    piece("C", "5", 13000, 1),
    piece("C", "6", 4000, 6),
    piece("D", "7", 2500, 8),
]


def settings_(**changes: object) -> OptimizerSettings:
    values: dict[str, object] = {
        "stock_lengths_mm": (10000, 12000),
        "kerf_mm": 3,
        "time_limit_s": 10,
        "num_workers": 2,
    }
    return OptimizerSettings(**{**values, **changes})  # type: ignore[arg-type]


def totals(plan: CuttingPlan) -> list[tuple[object, ...]]:
    """What must not depend on how the sizes were scheduled."""
    rows = []
    for size_plan in plan.sizes:
        summary = summarize_size(size_plan)
        rows.append(
            (
                size_plan.size,
                size_plan.status,
                summary.new_stock_mm,
                summary.inventory_mm,
                summary.pieces_mm,
                summary.piece_count,
                size_plan.lower_bound_mm,
                [(u.no, u.quantity) for u in size_plan.unplaced],
            )
        )
    return rows


# --- resources ----------------------------------------------------------------------------


@pytest.mark.parametrize("cpus", [1, 2, 4, 8, 16, 64])
def test_solver_threads_are_never_reduced(cpus: int) -> None:
    for mode in PerformanceMode:
        resources = plan_resources(mode, cpus)
        assert resources.num_workers == min(8, cpus)
        assert 1 <= resources.size_workers <= max(1, cpus // 4)
    assert plan_resources(PerformanceMode.ECO, cpus).size_workers == 1


def test_default_is_two_workers_and_manual_count_is_kept() -> None:
    assert plan_resources(PerformanceMode.BALANCED, 16).size_workers == 2
    assert plan_resources(PerformanceMode.BALANCED, 8).size_workers == 2
    assert plan_resources(PerformanceMode.PERFORMANCE, 16).size_workers == 4
    assert plan_resources(PerformanceMode.PERFORMANCE, 8).size_workers == 2
    assert plan_resources(PerformanceMode.BALANCED, 4).size_workers == 1  # too small to share
    assert plan_resources(PerformanceMode.ECO, 16, size_workers=6).size_workers == 6
    assert plan_resources(PerformanceMode.ECO, 4, size_workers=99).size_workers == 4


# --- same result as one by one ------------------------------------------------------------


@pytest.mark.parametrize("mode", list(StockMode))
def test_parallel_matches_sequential(mode: StockMode) -> None:
    inventory = [InventoryStock("B", 7000, 2, (2,))]
    sequential = planner.plan(PIECES, settings_(stock_mode=mode), inventory=inventory)
    events: list[ProgressEvent] = []
    result = planner.plan(
        PIECES, settings_(stock_mode=mode, size_workers=3), events.append, inventory=inventory
    )
    assert [s.size for s in result.sizes] == ["A", "B", "C", "D"]  # input order kept
    assert totals(result) == totals(sequential)
    assert [i.code for i in result.issues] == [i.code for i in sequential.issues]

    fractions = [e.fraction for e in events]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    for size in "ABCD":
        kinds = [e.kind for e in events if e.size == size]
        assert kinds[0] is ProgressKind.SIZE_STARTED and kinds[-1] is ProgressKind.SIZE_FINISHED
        assert kinds.count(ProgressKind.SIZE_STARTED) == 1
    assert {e.worker for e in events} <= {0, 1, 2}
    assert len({e.worker for e in events}) > 1


def test_parallel_matches_sequential_on_generated_pieces() -> None:
    pieces = make_pieces(PRESETS["small"])
    sequential = planner.plan(pieces, settings_(kerf_mm=0))
    result = planner.plan(pieces, settings_(kerf_mm=0, size_workers=2))
    assert totals(result) == totals(sequential)


def test_one_size_or_one_worker_stays_in_this_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_processes(*_: object, **__: object) -> None:
        raise AssertionError("worker processes must not be used")

    monkeypatch.setattr(parallel, "run_sizes", no_processes)
    planner.plan(PIECES, settings_(size_workers=1))
    planner.plan(PIECES[:2], settings_(size_workers=4))


# --- failures, cancel, hangs ----------------------------------------------------------------


def tasks_(count: int) -> list[SizeTask]:
    return [SizeTask(i, f"S{i}", (piece(f"S{i}", str(i), 3000, 4),), {}) for i in range(count)]


def fail_on_s1(
    task: SizeTask, settings: OptimizerSettings, cancel: CancelToken, on_stage: StageCallback
) -> SizeResult:
    if task.size == "S1":
        raise ValueError("boom")
    return solve_task(task, settings, cancel, on_stage)


def die_on_s1(
    task: SizeTask, settings: OptimizerSettings, cancel: CancelToken, on_stage: StageCallback
) -> SizeResult:
    if task.size == "S1":
        os._exit(7)
    return solve_task(task, settings, cancel, on_stage)


def hang_on_s1(
    task: SizeTask, settings: OptimizerSettings, cancel: CancelToken, on_stage: StageCallback
) -> SizeResult:
    if task.size == "S1":
        time.sleep(600)
    return solve_task(task, settings, cancel, on_stage)


def wait_for_cancel(
    task: SizeTask, settings: OptimizerSettings, cancel: CancelToken, on_stage: StageCallback
) -> SizeResult:
    while True:
        cancel.raise_if_cancelled()
        time.sleep(0.02)


def ignore_cancel(
    task: SizeTask, settings: OptimizerSettings, cancel: CancelToken, on_stage: StageCallback
) -> SizeResult:
    time.sleep(600)
    raise AssertionError


@pytest.mark.parametrize(
    ("solve", "reason"),
    [(fail_on_s1, "ValueError: boom"), (die_on_s1, "exit code 7"), (hang_on_s1, "no result after")],
)
def test_a_failing_size_does_not_stop_the_others(
    solve: parallel.Solve, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(parallel, "HANG_FACTOR", 0.0)
    monkeypatch.setattr(parallel, "HANG_EXTRA_S", 6.0)
    events: list[ProgressEvent] = []
    with pytest.raises(PlanningFailed) as info:
        parallel.run_sizes(tasks_(5), settings_(size_workers=2), events.append, solve=solve)
    issues = info.value.issues
    assert [(i.code, i.params["size"]) for i in issues] == [(IssueCode.SIZE_FAILED, "S1")]
    assert reason in str(issues[0].params["reason"])
    assert th.format_issue(issues[0]).startswith("Size S1: คำนวณไม่สำเร็จ")
    finished = {e.size for e in events if e.kind is ProgressKind.SIZE_FINISHED}
    assert finished == {"S0", "S2", "S3", "S4"}  # the replacement worker took over the queue
    failed = [e for e in events if e.kind is ProgressKind.SIZE_FAILED]
    assert [e.size for e in failed] == ["S1"] and reason in failed[0].message
    assert events[-1].fraction == 1.0


@pytest.mark.parametrize("solve", [wait_for_cancel, ignore_cancel])
def test_cancel_stops_running_and_queued_sizes(
    solve: parallel.Solve, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(parallel, "CANCEL_GRACE_S", 1.0)
    token = CancelToken()
    events: list[ProgressEvent] = []
    run = parallel._Run(tasks_(6), settings_(size_workers=2), events.append, token, solve)
    threading.Timer(2.0, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        run.run()
    assert time.monotonic() - started < 15
    assert not any(worker.process.is_alive() for worker in run.workers)
    assert {e.size for e in events if e.kind is ProgressKind.SIZE_STARTED} == {"S0", "S1"}
    assert len(run.pending) == 4  # queued sizes were never started


def test_workers_are_gone_after_a_normal_run() -> None:
    run = parallel._Run(tasks_(3), settings_(size_workers=2), None, None, solve_task)
    results = run.run()
    assert [size_plan.size for size_plan, _ in results] == ["S0", "S1", "S2"]
    assert not any(worker.process.is_alive() for worker in run.workers)


def test_cancelled_before_start_raises_from_plan() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        planner.plan(PIECES, settings_(size_workers=2), cancel=token)
