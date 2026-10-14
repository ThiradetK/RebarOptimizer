"""Core domain models. All lengths are integer millimetres (D1).

Plans store only raw facts (which pieces on which stock). Kerf loss, remnant
and totals are derived by ``solver.kerf`` / ``services.summary`` so that they
can never drift from the raw plan (D7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rebar_optimizer.domain.errors import Issue
    from rebar_optimizer.domain.settings import OptimizerSettings

PieceKey = tuple[str, str]
"""``(size, no)``: the identity of a piece (D6)."""


@dataclass(frozen=True)
class Piece:
    """One input line after normalization and duplicate merging."""

    size: str
    no: str
    length_mm: int
    quantity: int
    rows: tuple[int, ...]
    """Excel row numbers this piece came from (header = 1, first data row = 2)."""

    @property
    def key(self) -> PieceKey:
        return (self.size, self.no)


class StockSource(StrEnum):
    NEW = "new"
    INVENTORY = "inventory"


@dataclass(frozen=True)
class StockOption:
    length_mm: int
    available: int | None = None
    """``None`` means unlimited."""
    source: StockSource = StockSource.NEW


@dataclass(frozen=True)
class InventoryStock:
    """Bars already on hand for one size (optional ``Stock`` sheet, phase 8)."""

    size: str
    length_mm: int
    quantity: int
    rows: tuple[int, ...]
    """Excel row numbers of the ``Stock`` sheet this entry came from."""


@dataclass(frozen=True)
class Cut:
    """One piece cut from a bar."""

    no: str
    length_mm: int


@dataclass(frozen=True)
class Bar:
    """One stock bar and the pieces cut from it, in cutting order.

    ``cuts`` is sorted longest first. Kerf loss and remnant are derived by
    ``solver.kerf``, never stored.
    """

    size: str
    stock_length_mm: int
    cuts: tuple[Cut, ...]
    source: StockSource = StockSource.NEW
    """``INVENTORY`` bars come from the ``Stock`` sheet and are not purchased."""

    @property
    def used_length_mm(self) -> int:
        return sum(c.length_mm for c in self.cuts)


@dataclass(frozen=True)
class BarGroup:
    """``count`` identical bars."""

    bar: Bar
    count: int


@dataclass(frozen=True)
class Unplaced:
    """Pieces that cannot be cut from any stock length."""

    size: str
    no: str
    length_mm: int
    quantity: int
    longest_stock_mm: int


@dataclass(frozen=True)
class Remnant:
    """Reusable remnants of one length (D5 step 4)."""

    length_mm: int
    count: int
    longest_cuttable_mm: int
    """Longest piece length of the size that can still be cut from it."""


class SolveStatus(StrEnum):
    """Quality of a size's result, best first (D8)."""

    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    HEURISTIC = "HEURISTIC"
    GREEDY = "GREEDY"

    @property
    def rank(self) -> int:
        """0 = best."""
        return list(SolveStatus).index(self)


@dataclass(frozen=True)
class SizePlan:
    size: str
    kerf_mm: int
    groups: tuple[BarGroup, ...]
    unplaced: tuple[Unplaced, ...]
    remnants: tuple[Remnant, ...]
    piece_lengths_mm: tuple[int, ...]
    """Distinct input lengths of this size, shortest first (classifies remnants)."""
    status: SolveStatus
    lower_bound_mm: int | None
    """Bound on the purchased (new) stock length; equals the total without inventory."""
    single_stock_mm: int | None = None
    """The one new stock length chosen in single-length mode, else ``None``."""

    @property
    def bar_count(self) -> int:
        return sum(g.count for g in self.groups)

    @property
    def total_stock_mm(self) -> int:
        return sum(g.bar.stock_length_mm * g.count for g in self.groups)

    @property
    def new_stock_mm(self) -> int:
        """Stock to purchase: the primary objective (D2, phase 8)."""
        return sum(
            g.bar.stock_length_mm * g.count for g in self.groups if g.bar.source is StockSource.NEW
        )

    @property
    def inventory_stock_mm(self) -> int:
        return self.total_stock_mm - self.new_stock_mm

    @property
    def total_pieces_mm(self) -> int:
        return sum(g.bar.used_length_mm * g.count for g in self.groups)

    @property
    def gap(self) -> float | None:
        """``(result - bound) / result`` on the purchased length, or ``None`` without a bound."""
        total = self.new_stock_mm
        if self.lower_bound_mm is None or total == 0:
            return None if self.lower_bound_mm is None else 0.0
        return max(0.0, (total - self.lower_bound_mm) / total)


@dataclass(frozen=True)
class CuttingPlan:
    sizes: tuple[SizePlan, ...]
    settings: OptimizerSettings
    issues: tuple[Issue, ...] = ()
    """Warnings produced while planning (D10 codes)."""

    @property
    def unplaced(self) -> tuple[Unplaced, ...]:
        return tuple(u for s in self.sizes for u in s.unplaced)

    @property
    def worst_status(self) -> SolveStatus | None:
        statuses = [s.status for s in self.sizes if s.groups]
        return max(statuses, key=lambda st: st.rank) if statuses else None
