"""Solver interface. Solvers work at the "length" level: pieces are plain
lengths in mm; mapping lengths back to piece numbers happens in the planner.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from rebar_optimizer.domain.models import SolveStatus
from rebar_optimizer.services.cancel import CancelToken

Demand = Mapping[int, int]
"""``{length_mm: count}`` for one size."""


@dataclass(frozen=True)
class RawBar:
    """One bar at the length level. ``lengths`` is sorted longest first."""

    stock_length_mm: int
    lengths: tuple[int, ...]

    @staticmethod
    def of(stock_length_mm: int, lengths: Iterable[int]) -> RawBar:
        return RawBar(stock_length_mm, tuple(sorted(lengths, reverse=True)))


@dataclass(frozen=True)
class SolveResult:
    bars: tuple[RawBar, ...]
    status: SolveStatus
    lower_bound_mm: int | None = None

    @property
    def total_stock_mm(self) -> int:
        return total_stock_mm(self.bars)


def total_stock_mm(bars: Iterable[RawBar]) -> int:
    return sum(bar.stock_length_mm for bar in bars)


def cut_counts(bars: Iterable[RawBar]) -> Counter[int]:
    counts: Counter[int] = Counter()
    for bar in bars:
        counts.update(bar.lengths)
    return counts


class Solver(Protocol):
    def solve(
        self,
        demand: Demand,
        stock_lengths_mm: tuple[int, ...],
        kerf_mm: int,
        cancel: CancelToken | None = None,
    ) -> SolveResult: ...
