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
    SIZE_FINISHED = "size_finished"


@dataclass(frozen=True)
class ProgressEvent:
    kind: ProgressKind
    size: str
    index: int
    """0-based position of ``size``."""
    total: int
    """Number of sizes."""

    @property
    def fraction(self) -> float:
        """Overall progress in ``[0, 1]``."""
        done = self.index + (1 if self.kind is ProgressKind.SIZE_FINISHED else 0)
        return done / self.total if self.total else 1.0


ProgressCallback = Callable[[ProgressEvent], None]
