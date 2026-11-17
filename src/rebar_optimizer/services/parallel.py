"""Plan several sizes at once, each in its own worker process (D9).

Sizes are independent: pieces and inventory are split per size before
planning, and every worker runs the unchanged ``planner.plan_size``. Processes
rather than threads because greedy and post-processing are pure Python.

The parent owns everything shared. It hands one size at a time to each idle
worker over that worker's own pipe (so a killed worker can corrupt nothing
shared), turns the workers' messages into ``ProgressEvent``s, and returns the
results in size order. A size that raises, whose worker dies, or that runs far
past its time limit is reported as failed and its worker replaced; the other
sizes carry on. Cancelling sets a flag every worker polls; workers that do not
stop in time are terminated. No worker outlives the parent.
"""

from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
import sys
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.connection import wait as wait_ready
from multiprocessing.process import BaseProcess
from typing import Any

from rebar_optimizer.domain.errors import Cancelled, Issue, IssueCode, PlanningFailed
from rebar_optimizer.domain.models import Piece, SizePlan
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import (
    CancelToken,
    ProgressCallback,
    ProgressEvent,
    ProgressKind,
    Stage,
    StageCallback,
)
from rebar_optimizer.solver.base import Inventory

logger = logging.getLogger(__name__)

POLL_S = 0.1
"""How often the parent looks at the cancel flag and at hung workers."""
CANCEL_GRACE_S = 5.0
"""Workers get this long to stop by themselves after a cancel."""
EXIT_GRACE_S = 2.0
HANG_FACTOR = 4.0
HANG_EXTRA_S = 120.0
"""A size is given up when its worker sends nothing (no stage, no result) for
``time_limit_s * HANG_FACTOR + HANG_EXTRA_S``. The time limit is a target, not a
hard stop, so only far beyond it is a hang. Counting from the last message
rather than from the start keeps a large size that is still moving through
its stages from being given up."""


@dataclass(frozen=True)
class SizeTask:
    index: int
    size: str
    pieces: tuple[Piece, ...]
    inventory: Inventory


SizeResult = tuple[SizePlan, list[Issue]]
Solve = Callable[[SizeTask, OptimizerSettings, CancelToken, StageCallback], SizeResult]


def solve_task(
    task: SizeTask, settings: OptimizerSettings, cancel: CancelToken, on_stage: StageCallback
) -> SizeResult:
    issues: list[Issue] = []
    size_plan = planner.plan_size(
        task.size,
        task.pieces,
        settings,
        cancel,
        issues,
        inventory=task.inventory,
        on_stage=on_stage,
    )
    return size_plan, issues


# --- worker process ------------------------------------------------------------------------


def _lower_priority() -> None:
    """Below-normal priority: the desktop and other programs go first."""
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415 - Windows only
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        below_normal_priority_class = 0x00004000
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal_priority_class)
    else:
        with contextlib.suppress(OSError):
            os.nice(5)


def _exit_with_parent() -> None:
    """Never outlive the application, even if it is killed mid-computation."""
    parent = multiprocessing.parent_process()
    if parent is not None:
        parent.join()
        os._exit(1)


def worker_main(
    conn: Connection, cancel_event: Any, settings: OptimizerSettings, solve: Solve
) -> None:
    """Loop of one worker: receive a ``SizeTask``, plan it, send the outcome."""
    _lower_priority()
    threading.Thread(target=_exit_with_parent, name="parent-watch", daemon=True).start()
    token = CancelToken(cancel_event)
    while True:
        try:
            task = conn.recv()
        except (EOFError, OSError):
            return
        if task is None:
            return
        index = task.index

        def on_stage(stage: Stage, step: int, steps: int, index: int = index) -> None:
            conn.send(("stage", index, stage, step, steps))

        try:
            size_plan, issues = solve(task, settings, token, on_stage)
            conn.send(("done", index, size_plan, issues))
        except Cancelled:
            conn.send(("cancelled", index))
            return
        except Exception as exc:
            conn.send(("failed", index, f"{type(exc).__name__}: {exc}", traceback.format_exc()))


# --- parent --------------------------------------------------------------------------------


class _Worker:
    def __init__(
        self,
        number: int,
        context: Any,
        cancel_event: Any,
        settings: OptimizerSettings,
        solve: Solve,
    ) -> None:
        self.number = number
        self.conn, child = context.Pipe()
        self.process: BaseProcess = context.Process(
            target=worker_main,
            args=(child, cancel_event, settings, solve),
            name=f"size-worker-{number}",
            daemon=True,
        )
        self.process.start()
        child.close()
        self.task: SizeTask | None = None
        self.last_heard = 0.0

    def stop(self, grace_s: float) -> None:
        """Ask an idle worker to leave; terminate whatever is left after ``grace_s``."""
        with contextlib.suppress(OSError, ValueError):
            self.conn.send(None)
        self.process.join(grace_s)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(EXIT_GRACE_S)
        with contextlib.suppress(OSError):
            self.conn.close()


class _Run:
    def __init__(
        self,
        tasks: Sequence[SizeTask],
        settings: OptimizerSettings,
        on_progress: ProgressCallback | None,
        cancel: CancelToken | None,
        solve: Solve,
    ) -> None:
        self.tasks = tasks
        self.settings = settings
        self.on_progress = on_progress
        self.cancel = cancel
        self.solve = solve
        self.context = multiprocessing.get_context("spawn")
        self.cancel_event = self.context.Event()
        self.pending = deque(tasks)
        self.results: dict[int, SizeResult] = {}
        self.failures: dict[int, str] = {}
        self.within = [0.0] * len(tasks)
        self.hang_after_s = settings.time_limit_s * HANG_FACTOR + HANG_EXTRA_S
        count = min(settings.size_workers, len(tasks))
        self.workers = [self._spawn(number) for number in range(count)]

    def _spawn(self, number: int) -> _Worker:
        return _Worker(number, self.context, self.cancel_event, self.settings, self.solve)

    # --- progress ---

    def _emit(self, kind: ProgressKind, task: SizeTask, worker: _Worker, **fields: Any) -> None:
        event = ProgressEvent(
            kind, task.size, task.index, len(self.tasks), worker=worker.number, **fields
        )
        # Sizes overlap, so overall progress is the sum over sizes (never decreasing).
        self.within[task.index] = max(self.within[task.index], event.within)
        if self.on_progress is not None:
            overall = sum(self.within) / len(self.tasks)
            self.on_progress(
                ProgressEvent(
                    kind,
                    task.size,
                    task.index,
                    len(self.tasks),
                    worker=worker.number,
                    overall=overall,
                    **fields,
                )
            )

    # --- one worker ---

    def _assign(self, worker: _Worker) -> None:
        task = self.pending.popleft()
        worker.task = task
        worker.last_heard = time.monotonic()
        self._emit(ProgressKind.SIZE_STARTED, task, worker)
        with contextlib.suppress(OSError, ValueError):  # worker gone: _check reports it
            worker.conn.send(task)

    def _fail(self, worker: _Worker, reason: str, detail: str = "") -> None:
        task = worker.task
        assert task is not None
        logger.error(
            "size %s failed in worker %d: %s\n%s", task.size, worker.number, reason, detail
        )
        self.failures[task.index] = reason
        worker.task = None
        self._emit(ProgressKind.SIZE_FAILED, task, worker, message=reason)

    def _handle(self, worker: _Worker, message: tuple[Any, ...]) -> None:
        task = worker.task
        if task is None or message[1] != task.index:
            raise RuntimeError(f"worker {worker.number} answered for an unexpected size")
        if message[0] == "stage":
            _, _, stage, step, steps = message
            self._emit(ProgressKind.STAGE, task, worker, stage=stage, step=step, steps=steps)
        elif message[0] == "done":
            _, _, size_plan, issues = message
            if not isinstance(size_plan, SizePlan) or size_plan.size != task.size:
                self._fail(worker, "worker returned a plan for another size")
                return
            self.results[task.index] = (size_plan, list(issues))
            worker.task = None
            self._emit(ProgressKind.SIZE_FINISHED, task, worker)
        elif message[0] == "failed":
            self._fail(worker, message[2], message[3])
        elif message[0] == "cancelled":
            raise Cancelled

    def _check(self, worker: _Worker) -> None:
        """Read what the worker sent; replace it if it died or hangs."""
        broken = False
        while worker.task is not None:
            try:
                if not worker.conn.poll():
                    break
                message = worker.conn.recv()
            except (EOFError, OSError):
                broken = True
                break
            worker.last_heard = time.monotonic()
            self._handle(worker, message)
        if worker.task is None:
            return
        if broken or not worker.process.is_alive():
            worker.process.join(EXIT_GRACE_S)  # the pipe closes just before the exit code is set
            self._fail(
                worker, f"worker process ended unexpectedly (exit code {worker.process.exitcode})"
            )
        elif time.monotonic() - worker.last_heard > self.hang_after_s:
            self._fail(worker, f"no progress for {self.hang_after_s:.0f} s")
        else:
            return
        worker.stop(0.0)
        self.workers[self.workers.index(worker)] = self._spawn(worker.number)

    # --- the run ---

    def run(self) -> list[SizeResult]:
        try:
            while len(self.results) + len(self.failures) < len(self.tasks):
                if self.cancel is not None and self.cancel.is_cancelled:
                    raise Cancelled
                for worker in self.workers:
                    if worker.task is None and self.pending:
                        self._assign(worker)
                busy = [w for w in self.workers if w.task is not None]
                wait_ready(
                    [w.conn for w in busy] + [w.process.sentinel for w in busy], timeout=POLL_S
                )
                for worker in busy:
                    self._check(worker)
        except Cancelled:
            self.cancel_event.set()
            self._shutdown(CANCEL_GRACE_S)
            raise
        except BaseException:
            self.cancel_event.set()
            self._shutdown(0.0)
            raise
        self._shutdown(EXIT_GRACE_S)
        if self.failures:
            raise PlanningFailed(
                Issue.error(IssueCode.SIZE_FAILED, size=self.tasks[index].size, reason=reason)
                for index, reason in sorted(self.failures.items())
            )
        missing = [task.size for task in self.tasks if task.index not in self.results]
        if missing:  # pragma: no cover - the loop only ends when every size is accounted for
            raise RuntimeError(f"sizes without a result: {missing}")
        return [self.results[task.index] for task in self.tasks]

    def _shutdown(self, grace_s: float) -> None:
        deadline = time.monotonic() + grace_s
        for worker in self.workers:
            worker.stop(max(0.0, deadline - time.monotonic()))


def run_sizes(
    tasks: Sequence[SizeTask],
    settings: OptimizerSettings,
    on_progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    solve: Solve = solve_task,
) -> list[SizeResult]:
    """Plan ``tasks`` on ``settings.size_workers`` processes; results in task order.

    Raises ``Cancelled``, or ``PlanningFailed`` naming every size that failed
    (after the other sizes have finished).
    """
    return _Run(tasks, settings, on_progress, cancel, solve).run()
