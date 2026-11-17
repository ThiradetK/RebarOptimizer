"""Optimizer settings shared by CLI and GUI."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from enum import StrEnum

from rebar_optimizer.domain.errors import Issue, IssueCode, SettingsError
from rebar_optimizer.domain.models import StockOption

KERF_MIN_MM = 0
KERF_MAX_MM = 20


class StockMode(StrEnum):
    MIXED = "mixed"
    """Solver may mix all stock lengths freely (default)."""
    SINGLE = "single"
    """Each size uses exactly one stock length (D4)."""


MAX_SOLVER_THREADS = 8
"""CP-SAT gains little beyond this many threads on these models."""


def default_num_workers() -> int:
    return min(MAX_SOLVER_THREADS, os.cpu_count() or 1)


class PerformanceMode(StrEnum):
    """How many sizes may be planned at the same time."""

    ECO = "eco"
    """One size at a time."""
    BALANCED = "balanced"
    PERFORMANCE = "performance"


@dataclass(frozen=True)
class Resources:
    size_workers: int
    """Sizes planned at the same time (1 = one after another)."""
    num_workers: int
    """CP-SAT threads inside each size."""


CPUS_PER_SIZE = 4
"""Logical processors a size needs for itself. A size's ``MAX_SOLVER_THREADS``
solver threads keep five to six of them busy on average (measured), so one
size per four processors is where planning side by side stops paying off."""
MODE_SIZE_LIMIT = {
    PerformanceMode.ECO: 1,
    PerformanceMode.BALANCED: 2,
    PerformanceMode.PERFORMANCE: 4,
}


def plan_resources(
    mode: PerformanceMode, logical_cpus: int | None = None, size_workers: int | None = None
) -> Resources:
    """How many sizes to plan side by side on this machine; ``size_workers``
    overrides the automatic choice (at most one per logical processor).

    The solver threads per size are never reduced: with fewer than
    ``MAX_SOLVER_THREADS`` CP-SAT drops the sub-solvers that prove optimality
    (the medium benchmark went from 6 s, all optimal, to 58 s with sizes left
    unproven at 4 threads). The modes only choose how many sizes overlap.
    """
    cpus = max(1, logical_cpus if logical_cpus is not None else os.cpu_count() or 1)
    if size_workers is None:
        size_workers = min(MODE_SIZE_LIMIT[mode], cpus // CPUS_PER_SIZE)
    return Resources(
        size_workers=max(1, min(size_workers, cpus)),
        num_workers=min(MAX_SOLVER_THREADS, cpus),
    )


@dataclass(frozen=True)
class OptimizerSettings:
    stock_lengths_mm: tuple[int, ...]
    stock_mode: StockMode = StockMode.MIXED
    kerf_mm: int = 0
    time_limit_s: float = 30.0
    max_patterns: int = 50_000
    random_seed: int = 0
    num_workers: int = field(default_factory=default_num_workers)
    """CP-SAT threads per size."""
    size_workers: int = 1
    """Sizes planned at the same time, each in its own process; 1 plans them
    one after another in the calling process (D9)."""

    def validate(self) -> None:
        """Raise ``SettingsError`` listing every invalid setting."""
        issues: list[Issue] = []
        if not self.stock_lengths_mm:
            issues.append(Issue.error(IssueCode.STOCK_EMPTY))
        seen: set[int] = set()
        for length in self.stock_lengths_mm:
            if length <= 0:
                issues.append(Issue.error(IssueCode.STOCK_NOT_POSITIVE, length_mm=length))
            elif length in seen:
                issues.append(Issue.error(IssueCode.STOCK_DUPLICATE, length_mm=length))
            seen.add(length)
        if not KERF_MIN_MM <= self.kerf_mm <= KERF_MAX_MM:
            issues.append(
                Issue.error(
                    IssueCode.KERF_OUT_OF_RANGE,
                    kerf_mm=self.kerf_mm,
                    min_mm=KERF_MIN_MM,
                    max_mm=KERF_MAX_MM,
                )
            )
        if not (math.isfinite(self.time_limit_s) and self.time_limit_s > 0):
            issues.append(Issue.error(IssueCode.TIME_LIMIT_NOT_POSITIVE, value=self.time_limit_s))
        if self.max_patterns <= 0:
            issues.append(Issue.error(IssueCode.MAX_PATTERNS_NOT_POSITIVE, value=self.max_patterns))
        if self.num_workers <= 0:
            issues.append(Issue.error(IssueCode.NUM_WORKERS_NOT_POSITIVE, value=self.num_workers))
        if self.size_workers <= 0:
            issues.append(Issue.error(IssueCode.SIZE_WORKERS_NOT_POSITIVE, value=self.size_workers))
        if issues:
            raise SettingsError(issues)

    @property
    def stock_options(self) -> tuple[StockOption, ...]:
        """Stock lengths as unlimited new stock, shortest first."""
        return tuple(StockOption(length) for length in sorted(self.stock_lengths_mm))


def parse_stock_lengths_m(text: str) -> tuple[int, ...]:
    """Parse ``"10, 12"`` (metres, comma separated) into millimetres.

    Duplicates are dropped (first occurrence kept); empty items are ignored.
    Non-positive values are kept so that ``validate()`` reports them.
    Raises ``SettingsError`` if any item is not a number.
    """
    issues: list[Issue] = []
    result: list[int] = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            metres = float(item)
        except ValueError:
            metres = math.nan
        if not math.isfinite(metres):
            issues.append(Issue.error(IssueCode.STOCK_INVALID_NUMBER, text=item))
            continue
        length_mm = round(metres * 1000)
        if length_mm not in result:
            result.append(length_mm)
    if issues:
        raise SettingsError(issues)
    return tuple(result)
