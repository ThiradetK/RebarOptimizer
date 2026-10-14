"""Best-Fit Decreasing (5.2): fast, always places every piece."""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from rebar_optimizer.domain.models import SolveStatus
from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import Demand, RawBar, SolveResult, total_stock_mm


def expand(demand: Demand) -> list[int]:
    """Demand as individual pieces, longest first."""
    return sorted((length for length, count in demand.items() for _ in range(count)), reverse=True)


def best_fit_decreasing(
    pieces_desc: Sequence[int], new_bar_stock_mm: int, kerf_mm: int
) -> list[list[int]]:
    """Pack pieces (longest first) into bars of ``new_bar_stock_mm``.

    Each piece goes to the open bar with the least free capacity that still
    holds it; otherwise a new bar is opened. Raises ``ValueError`` if a piece
    is longer than the stock.
    """
    bar_capacity = kerf.capacity(new_bar_stock_mm, kerf_mm)
    bars: list[list[int]] = []
    open_by_free: list[tuple[int, int]] = []  # sorted (free capacity, bar index)
    for length in pieces_desc:
        need = kerf.item_size(length, kerf_mm)
        if need > bar_capacity:
            raise ValueError(f"piece {length} mm is longer than stock {new_bar_stock_mm} mm")
        pos = bisect.bisect_left(open_by_free, (need, -1))
        if pos < len(open_by_free):
            free, index = open_by_free.pop(pos)
        else:
            free, index = bar_capacity, len(bars)
            bars.append([])
        bars[index].append(length)
        bisect.insort(open_by_free, (free - need, index))
    return bars


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
    that holds the longest piece; the run with the least total stock wins
    (first run on ties, so results are deterministic).
    """

    def solve(
        self,
        demand: Demand,
        stock_lengths_mm: tuple[int, ...],
        kerf_mm: int,
        cancel: CancelToken | None = None,
    ) -> SolveResult:
        pieces = expand(demand)
        stocks = sorted(stock_lengths_mm)
        if not pieces:
            return SolveResult(bars=(), status=SolveStatus.GREEDY)
        longest_piece = pieces[0]
        runs = [stocks[-1]]
        if len(stocks) > 1:
            runs += [s for s in stocks[:-1] if s >= longest_piece]

        best: list[RawBar] | None = None
        for new_bar_stock in runs:
            check_cancelled(cancel)
            packed = best_fit_decreasing(pieces, new_bar_stock, kerf_mm)
            candidate = downsize(packed, stocks, kerf_mm)
            if best is None or total_stock_mm(candidate) < total_stock_mm(best):
                best = candidate
        assert best is not None
        return SolveResult(bars=tuple(best), status=SolveStatus.GREEDY)
