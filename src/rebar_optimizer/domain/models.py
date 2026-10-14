"""Core domain models. All lengths are integer millimetres (D1).

``SizePlan``, ``CuttingPlan`` and ``Remnant`` are added in phase 2 together
with the solver status they depend on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
class Cut:
    """One piece cut from a bar."""

    no: str
    length_mm: int


@dataclass(frozen=True)
class Bar:
    """One stock bar and the pieces cut from it, in cutting order.

    Derived quantities (kerf loss, remnant) are computed by ``solver.kerf``,
    never stored, so they cannot drift from the raw plan (D7).
    """

    size: str
    stock_length_mm: int
    cuts: tuple[Cut, ...]

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
