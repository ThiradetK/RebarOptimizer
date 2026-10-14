"""Best-Fit Decreasing (5.2): fast, always places every piece."""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from rebar_optimizer.domain.models import SolveStatus, StockSource
from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import (
    NO_INVENTORY,
    Demand,
    Inventory,
    RawBar,
    SolveResult,
    expand_inventory,
    stock_cost,
)


def expand(demand: Demand) -> list[int]:
    """Demand as individual pieces, longest first."""
    return sorted((length for length, count in demand.items() for _ in range(count)), reverse=True)


def best_fit_decreasing(
    pieces_desc: Sequence[int],
    new_bar_stock_mm: int,
    kerf_mm: int,
    inventory_bars: Sequence[int] = (),
) -> tuple[list[list[int]], list[list[int]]]:
    """Pack pieces (longest first) into bars of ``new_bar_stock_mm``.

    Each piece goes to the open bar with the least free capacity that still
    holds it; otherwise a new bar is opened. ``inventory_bars`` (lengths) start
    out open and empty, so they fill before any new bar is bought.

    Returns ``(new bars, pieces per inventory bar)``; the second list is aligned
    with ``inventory_bars`` and unused inventory bars stay empty. Raises
    ``ValueError`` if a piece fits neither the new stock nor any inventory bar.
    """
    bar_capacity = kerf.capacity(new_bar_stock_mm, kerf_mm)
    bins: list[list[int]] = [[] for _ in inventory_bars]
    # Sorted (free capacity, bin index); bins below len(inventory_bars) are inventory.
    open_by_free = sorted(
        (kerf.capacity(length, kerf_mm), index) for index, length in enumerate(inventory_bars)
    )
    for length in pieces_desc:
        need = kerf.item_size(length, kerf_mm)
        pos = bisect.bisect_left(open_by_free, (need, -1))
        if pos < len(open_by_free):
            free, index = open_by_free.pop(pos)
        elif need <= bar_capacity:
            free, index = bar_capacity, len(bins)
            bins.append([])
        else:
            raise ValueError(f"piece {length} mm is longer than stock {new_bar_stock_mm} mm")
        bins[index].append(length)
        bisect.insort(open_by_free, (free - need, index))
    split = len(inventory_bars)
    return bins[split:], bins[:split]


def downsize(
    bars: Sequence[Sequence[int]], stock_lengths_mm: Sequence[int], kerf_mm: int
) -> list[RawBar]:
    """Give each bar the shortest allowed stock that holds its pieces."""
    result: list[RawBar] = []
    for lengths in bars:
        stock = kerf.shortest_fitting_stock(lengths, stock_lengths_mm, kerf_mm)
        if stock is None:
            raise ValueError(f"pieces {list(lengths)} fit no stock length")
        result.append(RawBar.of(stock, lengths))
    return result


class GreedySolver:
    """BFD opening new bars with the longest stock, then downsizing.

    With several stock lengths, BFD is also run once per single stock length
    that holds the longest piece; the run with the lowest ``stock_cost`` wins
    (first run on ties, so results are deterministic). Inventory bars are
    filled first in every run; ``stock_lengths_mm`` must hold every piece.
    """

    def solve(
        self,
        demand: Demand,
        stock_lengths_mm: tuple[int, ...],
        kerf_mm: int,
        cancel: CancelToken | None = None,
        *,
        inventory: Inventory = NO_INVENTORY,
    ) -> SolveResult:
        pieces = expand(demand)
        stocks = sorted(stock_lengths_mm)
        if not pieces:
            return SolveResult(bars=(), status=SolveStatus.GREEDY)
        longest_piece = pieces[0]
        runs = [stocks[-1]]
        if len(stocks) > 1:
            runs += [s for s in stocks[:-1] if s >= longest_piece]
        inventory_bars = expand_inventory(inventory)

        best: list[RawBar] | None = None
        for new_bar_stock in runs:
            check_cancelled(cancel)
            packed, in_inventory = best_fit_decreasing(
                pieces, new_bar_stock, kerf_mm, inventory_bars
            )
            candidate = downsize(packed, stocks, kerf_mm) + [
                RawBar.of(stock, lengths, StockSource.INVENTORY)
                for stock, lengths in zip(inventory_bars, in_inventory, strict=True)
                if lengths
            ]
            if best is None or stock_cost(candidate) < stock_cost(best):
                best = candidate
        assert best is not None
        return SolveResult(bars=tuple(best), status=SolveStatus.GREEDY)
