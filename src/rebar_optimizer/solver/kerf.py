"""The single implementation of the kerf (saw width) rule (D3).

Pieces ``l_1..l_n`` (total ``L``) fit a stock bar ``S`` with kerf ``k`` iff
``L + k*(n-1) <= S``, equivalently ``sum(l_i + k) <= S + k`` (the knapsack form).
With ``r = S - L - k*(n-1)``:

* ``r == 0``      -> n-1 cuts, remnant 0,      kerf loss ``k*(n-1)``
* ``0 < r <= k``  -> n cuts,   remnant 0,      kerf loss ``k*(n-1) + r``
* ``r > k``       -> n cuts,   remnant ``r-k``, kerf loss ``k*n``

so ``L + kerf_loss + remnant == S`` always holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class BarUsage:
    stock_length_mm: int
    piece_count: int
    pieces_mm: int
    cut_count: int
    kerf_loss_mm: int
    remnant_mm: int


def slack_mm(lengths: Sequence[int], stock_length_mm: int, kerf_mm: int) -> int:
    """``r = S - L - k*(n-1)``; negative means the pieces do not fit."""
    if not lengths:
        return stock_length_mm
    return stock_length_mm - sum(lengths) - kerf_mm * (len(lengths) - 1)


def fits(lengths: Sequence[int], stock_length_mm: int, kerf_mm: int) -> bool:
    return slack_mm(lengths, stock_length_mm, kerf_mm) >= 0


def item_size(length_mm: int, kerf_mm: int) -> int:
    """Size of one piece in the knapsack form: ``l + k``."""
    return length_mm + kerf_mm


def capacity(stock_length_mm: int, kerf_mm: int) -> int:
    """Capacity of one bar in the knapsack form: ``S + k``."""
    return stock_length_mm + kerf_mm


def free_capacity(lengths: Iterable[int], stock_length_mm: int, kerf_mm: int) -> int:
    """Knapsack capacity left: a piece ``l`` still fits iff ``l + k <= free``.

    Equivalently ``free - k`` is the remnant once the last cut is made, so
    ``l <= free - k`` is "the remnant can still yield a piece of length l".
    """
    return capacity(stock_length_mm, kerf_mm) - sum(
        item_size(length, kerf_mm) for length in lengths
    )


def bar_usage(lengths: Sequence[int], stock_length_mm: int, kerf_mm: int) -> BarUsage:
    """Cuts, kerf loss and remnant of one bar. Raises ``ValueError`` if it does not fit."""
    n = len(lengths)
    if n == 0:
        raise ValueError("a bar must hold at least one piece")
    r = slack_mm(lengths, stock_length_mm, kerf_mm)
    if r < 0:
        raise ValueError(f"pieces {list(lengths)} do not fit {stock_length_mm} mm (kerf {kerf_mm})")
    if r == 0:
        cuts, kerf_loss, remnant = n - 1, kerf_mm * (n - 1), 0
    elif r <= kerf_mm:
        cuts, kerf_loss, remnant = n, kerf_mm * (n - 1) + r, 0
    else:
        cuts, kerf_loss, remnant = n, kerf_mm * n, r - kerf_mm
    return BarUsage(
        stock_length_mm=stock_length_mm,
        piece_count=n,
        pieces_mm=sum(lengths),
        cut_count=cuts,
        kerf_loss_mm=kerf_loss,
        remnant_mm=remnant,
    )


def shortest_fitting_stock(
    lengths: Sequence[int], stock_lengths_mm: Iterable[int], kerf_mm: int
) -> int | None:
    """Shortest stock length that holds ``lengths``, or ``None``."""
    candidates = [s for s in stock_lengths_mm if fits(lengths, s, kerf_mm)]
    return min(candidates) if candidates else None
