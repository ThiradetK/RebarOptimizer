"""Cancellation and progress reporting shared by CLI, GUI and the core (D9, D10)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from rebar_optimizer.domain.errors import Cancelled


class _Event(Protocol):
    def set(self) -> None: ...
    def is_set(self) -> bool: ...
    def wait(self, timeout: float | None = None) -> bool: ...


class CancelToken:
    """Thread-safe cancel flag. The GUI sets it; the core polls it.

    ``event``: share an existing flag, e.g. a ``multiprocessing.Event`` so that
    worker processes see a cancel made in the parent.
    """

    def __init__(self, event: _Event | None = None) -> None:
        self._event: _Event = event if event is not None else threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout_s: float) -> bool:
        """Block until cancelled or ``timeout_s`` elapses; return ``is_cancelled``."""
        return self._event.wait(timeout_s)

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled


def check_cancelled(token: CancelToken | None) -> None:
    if token is not None:
        token.raise_if_cancelled()


class ProgressKind(StrEnum):
    SIZE_STARTED = "size_started"
    STAGE = "stage"
    """A step inside one size (see ``Stage``)."""
    SIZE_FINISHED = "size_finished"
    SIZE_FAILED = "size_failed"
    """Parallel mode only: this size failed; the others carry on."""


class Stage(StrEnum):
    """Steps inside one size, in order. Long sizes otherwise show no progress."""

    GREEDY = "greedy"
    IMPROVE = "improve"
    SEARCH = "search"
    FINISH = "finish"


STAGE_OFFSETS: dict[Stage, float] = {
    Stage.GREEDY: 0.0,
    Stage.IMPROVE: 0.05,
    Stage.SEARCH: 0.15,
    Stage.FINISH: 0.95,
}
"""Where each stage starts within a size, as a share of that size's progress."""


@dataclass(frozen=True)
class ProgressEvent:
    kind: ProgressKind
    size: str
    index: int
    """0-based position of ``size``."""
    total: int
    """Number of sizes."""
    stage: Stage | None = None
    """Set for ``ProgressKind.STAGE``."""
    step: int = 0
    """0-based stock length tried, in single-length mode (otherwise 0)."""
    steps: int = 1
    """Number of stock lengths tried for this size."""
    worker: int = 0
    """0-based worker planning this size (always 0 when sizes run one by one)."""
    message: str = ""
    """Set for ``ProgressKind.SIZE_FAILED``: short English reason."""
    overall: float | None = None
    """Progress of the whole run when sizes overlap (parallel mode)."""

    @property
    def within(self) -> float:
        """Progress of this size alone in ``[0, 1]``."""
        if self.kind in {ProgressKind.SIZE_FINISHED, ProgressKind.SIZE_FAILED}:
            return 1.0
        if self.kind is ProgressKind.STAGE and self.stage is not None:
            if self.stage is Stage.FINISH:
                return STAGE_OFFSETS[Stage.FINISH]
            share = STAGE_OFFSETS[Stage.FINISH] / max(self.steps, 1)
            return share * (self.step + STAGE_OFFSETS[self.stage] / STAGE_OFFSETS[Stage.FINISH])
        return 0.0

    @property
    def fraction(self) -> float:
        """Overall progress in ``[0, 1]``; never goes backwards within a run."""
        if self.overall is not None:
            return self.overall
        if not self.total:
            return 1.0
        return (self.index + self.within) / self.total


ProgressCallback = Callable[[ProgressEvent], None]
StageCallback = Callable[[Stage, int, int], None]
"""``(stage, step, steps)`` reported inside a size; ``plan`` turns it into ``ProgressEvent``s."""
