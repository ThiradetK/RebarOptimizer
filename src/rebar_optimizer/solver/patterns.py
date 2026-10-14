"""Enumerate cutting patterns (5.3).

A pattern is a stock length plus how many pieces of each demanded length it
holds. Only *maximal* patterns are generated (no length with ``a_i < d_i``
still fits), and a pattern is skipped on a stock length when the same pieces fit
a shorter allowed length (that shorter pattern is generated instead). Restricting
the ILP to these patterns loses nothing: any plan can be padded to maximal
patterns (over-cutting, removed later) without using more stock.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import Demand, RawBar

_CANCEL_CHECK_EVERY = 4096


@dataclass(frozen=True)
class Pattern:
    stock_length_mm: int
    counts: tuple[int, ...]
    """Pieces per length, aligned with ``PatternSet.lengths``."""

    def lengths(self, lengths: Sequence[int]) -> list[int]:
        return [length for length, n in zip(lengths, self.counts, strict=True) for _ in range(n)]


@dataclass(frozen=True)
class PatternSet:
    lengths: tuple[int, ...]
    """Demanded lengths, longest first."""
    patterns: tuple[Pattern, ...]

    def to_raw_bar(self, pattern: Pattern) -> RawBar:
        return RawBar.of(pattern.stock_length_mm, pattern.lengths(self.lengths))


def iter_maximal_patterns(
    demand: Demand,
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    cancel: CancelToken | None = None,
) -> Iterator[Pattern]:
    """Yield every maximal, non-dominated pattern (lazily)."""
    lengths = tuple(sorted(demand, reverse=True))
    caps = tuple(demand[length] for length in lengths)
    weights = tuple(kerf.item_size(length, kerf_mm) for length in lengths)
    stocks = sorted(stock_lengths_mm)
    n = len(lengths)
    visited = 0

    # suffix_fill[i]: most capacity lengths i.. can still take (ignoring the bar).
    suffix_fill = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_fill[i] = suffix_fill[i + 1] + caps[i] * weights[i]
    no_limit = 1 << 62

    for stock_index, stock in enumerate(stocks):
        capacity = kerf.capacity(stock, kerf_mm)
        # Pieces fitting the next shorter stock belong to that stock instead.
        shorter_capacity = kerf.capacity(stocks[stock_index - 1], kerf_mm) if stock_index else -1
        counts = [0] * n
        # Iterative DFS. State: depth, remaining capacity, next count to try at
        # this depth (-1 = start at the maximum), smallest weight of an earlier
        # length left unsaturated although it fitted (maximality needs the final
        # remaining capacity to drop below it).
        stack: list[tuple[int, int, int, int]] = [(0, capacity, -1, no_limit)]
        while stack:
            depth, remaining, choice, unsaturated = stack.pop()
            visited += 1
            if visited % _CANCEL_CHECK_EVERY == 0:
                check_cancelled(cancel)
            if remaining - suffix_fill[depth] >= unsaturated:
                continue  # even taking every later piece leaves room for a skipped one
            if depth == n:
                used = capacity - remaining
                if (
                    used
                    and used > shorter_capacity
                    and _is_maximal(counts, caps, weights, remaining)
                ):
                    yield Pattern(stock, tuple(counts))
                continue
            most = min(caps[depth], remaining // weights[depth])
            if choice == -1:
                choice = most
            counts[depth] = choice
            if choice > 0:
                stack.append((depth, remaining, choice - 1, unsaturated))
            child_unsaturated = (
                min(unsaturated, weights[depth]) if choice < caps[depth] else unsaturated
            )
            stack.append((depth + 1, remaining - choice * weights[depth], -1, child_unsaturated))


def _is_maximal(
    counts: Sequence[int], caps: Sequence[int], weights: Sequence[int], remaining: int
) -> bool:
    return all(c >= cap or w > remaining for c, cap, w in zip(counts, caps, weights, strict=True))


def enumerate_patterns(
    demand: Demand,
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    max_patterns: int,
    *,
    extra_bars: Iterable[RawBar] = (),
    cancel: CancelToken | None = None,
) -> PatternSet | None:
    """All maximal patterns plus the patterns of ``extra_bars`` (e.g. the greedy
    plan, so the ILP can start from it). ``None`` if more than ``max_patterns``."""
    lengths = tuple(sorted(demand, reverse=True))
    found: dict[Pattern, None] = {}
    for pattern in iter_maximal_patterns(demand, stock_lengths_mm, kerf_mm, cancel):
        found[pattern] = None
        if len(found) > max_patterns:
            return None
    for bar in extra_bars:
        found.setdefault(pattern_of(bar, lengths), None)
    return PatternSet(lengths=lengths, patterns=tuple(found))


def pattern_of(bar: RawBar, lengths: Sequence[int]) -> Pattern:
    index = {length: i for i, length in enumerate(lengths)}
    counts = [0] * len(lengths)
    for length in bar.lengths:
        counts[index[length]] += 1
    return Pattern(bar.stock_length_mm, tuple(counts))
