"""Solver interface. Solvers work at the "length" level: pieces are plain
lengths in mm; mapping lengths back to piece numbers happens in the planner.

Stock comes from two sources (phase 8): new stock, unlimited, in the lengths
of the settings; and inventory (``{length_mm: bars available}``) from the
input's ``Stock`` sheet. Plans are compared by ``stock_cost``: purchased length
first, then inventory length used.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from rebar_optimizer.domain.models import SolveStatus, StockSource
from rebar_optimizer.services.cancel import CancelToken

Demand = Mapping[int, int]
"""``{length_mm: count}`` for one size."""

Inventory = Mapping[int, int]
"""``{length_mm: bars available}`` of one size's inventory."""

NO_INVENTORY: Inventory = {}


@dataclass(frozen=True)
class RawBar:
    """One bar at the length level. ``lengths`` is sorted longest first."""

    stock_length_mm: int
    lengths: tuple[int, ...]
    source: StockSource = StockSource.NEW

    @staticmethod
    def of(
        stock_length_mm: int, lengths: Iterable[int], source: StockSource = StockSource.NEW
    ) -> RawBar:
        return RawBar(stock_length_mm, tuple(sorted(lengths, reverse=True)), source)

    @property
    def is_inventory(self) -> bool:
        return self.source is StockSource.INVENTORY


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


def stock_cost(bars: Iterable[RawBar]) -> tuple[int, int]:
    """``(new mm, inventory mm)``; compare lexicographically (phase 8 objective).

    Without inventory this is ``(total stock, 0)``, i.e. the plain D2 objective.
    """
    new = inventory = 0
    for bar in bars:
        if bar.is_inventory:
            inventory += bar.stock_length_mm
        else:
            new += bar.stock_length_mm
    return new, inventory


def inventory_usage(bars: Iterable[RawBar]) -> Counter[int]:
    """``{length_mm: inventory bars used}``."""
    return Counter(bar.stock_length_mm for bar in bars if bar.is_inventory)


def expand_inventory(inventory: Inventory) -> list[int]:
    """Inventory as individual bar lengths, shortest first."""
    return sorted(length for length, count in inventory.items() for _ in range(count))


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
        *,
        inventory: Inventory = NO_INVENTORY,
    ) -> SolveResult: ...
