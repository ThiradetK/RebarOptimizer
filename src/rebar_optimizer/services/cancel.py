"""Cancellation and progress reporting shared by CLI, GUI and the core (D9, D10)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from rebar_optimizer.domain.errors import Cancelled


class CancelToken:
    """Thread-safe cancel flag. The GUI sets it; the core polls it."""

    def __init__(self) -> None:
        self._event = threading.Event()

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

    @property
    def fraction(self) -> float:
        """Overall progress in ``[0, 1]``; never goes backwards within a run."""
        if not self.total:
            return 1.0
        if self.kind is ProgressKind.SIZE_FINISHED:
            within = 1.0
        elif self.kind is ProgressKind.STAGE and self.stage is not None:
            if self.stage is Stage.FINISH:
                within = STAGE_OFFSETS[Stage.FINISH]
            else:
                share = STAGE_OFFSETS[Stage.FINISH] / max(self.steps, 1)
                within = share * (
                    self.step + STAGE_OFFSETS[self.stage] / STAGE_OFFSETS[Stage.FINISH]
                )
        else:
            within = 0.0
        return (self.index + within) / self.total


ProgressCallback = Callable[[ProgressEvent], None]
StageCallback = Callable[[Stage, int, int], None]
"""``(stage, step, steps)`` reported inside a size; ``plan`` turns it into ``ProgressEvent``s."""
