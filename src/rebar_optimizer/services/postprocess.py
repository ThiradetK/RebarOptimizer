"""Post-processing of a length-level plan, in the fixed order of D5.

1. remove over-cut pieces (ILP uses ``>=`` demand constraints)
2. downsize each bar to the shortest allowed stock that still holds it
3. reuse remnants: move pieces into other bars' remnants when that closes or
   downsizes the source bar (only strictly improving moves; repeat to a fixpoint)

Step 4 (classifying remnants) is ``classify_remnant``, applied when the plan is
summarized. No step ever increases the total stock length.
"""

from __future__ import annotations

import bisect
from collections import Counter
from collections.abc import Sequence

from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import Demand, RawBar, cut_counts, total_stock_mm


def postprocess(
    bars: Sequence[RawBar],
    demand: Demand,
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    cancel: CancelToken | None = None,
) -> list[RawBar]:
    before = total_stock_mm(bars)
    result = remove_overcuts(bars, demand, stock_lengths_mm, kerf_mm)
    result = downsize_all(result, stock_lengths_mm, kerf_mm)
    result = reuse_remnants(result, stock_lengths_mm, kerf_mm, cancel)
    if total_stock_mm(result) > before:  # pragma: no cover - guarded by construction
        raise AssertionError("post-processing increased total stock length")
    return result


# --- step 1 ----------------------------------------------------------------------


def remove_overcuts(
    bars: Sequence[RawBar], demand: Demand, stock_lengths_mm: Sequence[int], kerf_mm: int
) -> list[RawBar]:
    """Drop surplus pieces (D5 step 1): prefer (a) the bar whose stock can then
    shrink the most (vanishing counts as shrinking to 0), else (b) the bar that
    ends up with the longest remnant, so remnants concentrate in one bar."""
    result = list(bars)
    surplus = cut_counts(result) - Counter(demand)
    for length in sorted(surplus, reverse=True):
        for _ in range(surplus[length]):
            index = _bar_to_remove_from(result, length, stock_lengths_mm, kerf_mm)
            remaining = list(result[index].lengths)
            remaining.remove(length)
            if remaining:
                result[index] = RawBar.of(result[index].stock_length_mm, remaining)
            else:
                del result[index]
    return result


def _bar_to_remove_from(
    bars: Sequence[RawBar], length: int, stock_lengths_mm: Sequence[int], kerf_mm: int
) -> int:
    best_index = -1
    best_score: tuple[int, int] = (-1, -1)
    for index, bar in enumerate(bars):
        if length not in bar.lengths:
            continue
        remaining = list(bar.lengths)
        remaining.remove(length)
        if not remaining:
            score = (bar.stock_length_mm, 0)
        else:
            shorter = kerf.shortest_fitting_stock(remaining, stock_lengths_mm, kerf_mm)
            saving = max(0, bar.stock_length_mm - (shorter or bar.stock_length_mm))
            score = (saving, kerf.slack_mm(remaining, bar.stock_length_mm, kerf_mm))
        if score > best_score:
            best_index, best_score = index, score
    if best_index < 0:  # pragma: no cover - surplus implies presence
        raise AssertionError(f"no bar holds surplus length {length}")
    return best_index


# --- step 2 ----------------------------------------------------------------------


def downsize_all(
    bars: Sequence[RawBar], stock_lengths_mm: Sequence[int], kerf_mm: int
) -> list[RawBar]:
    result: list[RawBar] = []
    for bar in bars:
        stock = kerf.shortest_fitting_stock(bar.lengths, stock_lengths_mm, kerf_mm)
        if stock is None:
            raise ValueError(f"bar {bar} fits no allowed stock length")
        result.append(RawBar(stock, bar.lengths))
    return result


# --- step 3 ----------------------------------------------------------------------


def reuse_remnants(
    bars: Sequence[RawBar],
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    cancel: CancelToken | None = None,
) -> list[RawBar]:
    result = list(bars)
    while True:
        check_cancelled(cancel)
        improved = _close_one_bar(result, kerf_mm) or _downsize_one_bar(
            result, stock_lengths_mm, kerf_mm
        )
        if improved is None:
            return result
        result = improved


def _close_one_bar(bars: Sequence[RawBar], kerf_mm: int) -> list[RawBar] | None:
    """Move every piece of one bar into the remnants of the others."""
    free = [kerf.free_capacity(b.lengths, b.stock_length_mm, kerf_mm) for b in bars]
    by_free = sorted((f, i) for i, f in enumerate(free))
    total_free = sum(free)
    order = sorted(
        range(len(bars)), key=lambda i: (-bars[i].stock_length_mm, len(bars[i].lengths), i)
    )
    for source in order:
        needs = [kerf.item_size(length, kerf_mm) for length in bars[source].lengths]
        # Cheap rejections before simulating the placement.
        if sum(needs) > total_free - free[source]:
            continue
        largest_other = max((f for f, i in by_free[-2:] if i != source), default=-1)
        if largest_other < max(needs):
            continue
        others = [entry for entry in by_free if entry[1] != source]
        placement = _place_all(bars[source].lengths, others, kerf_mm)
        if placement is None:
            continue
        return [
            RawBar.of(bar.stock_length_mm, bar.lengths + tuple(placement[i]))
            if placement.get(i)
            else bar
            for i, bar in enumerate(bars)
            if i != source
        ]
    return None


def _place_all(
    lengths_desc: Sequence[int], free_sorted: list[tuple[int, int]], kerf_mm: int
) -> dict[int, list[int]] | None:
    """Best-fit pieces into ``(free capacity, bar index)`` slots (sorted; no new bars).

    Returns ``{bar index: pieces added}`` or ``None`` if some piece does not fit.
    """
    free = list(free_sorted)
    added: dict[int, list[int]] = {}
    for length in lengths_desc:
        need = kerf.item_size(length, kerf_mm)
        pos = bisect.bisect_left(free, (need, -1))
        if pos == len(free):
            return None
        capacity, index = free.pop(pos)
        added.setdefault(index, []).append(length)
        bisect.insort(free, (capacity - need, index))
    return added


def _downsize_one_bar(
    bars: Sequence[RawBar], stock_lengths_mm: Sequence[int], kerf_mm: int
) -> list[RawBar] | None:
    """Move one piece out of a bar into another bar's remnant so the source
    bar fits a shorter allowed stock."""
    for source, bar in enumerate(bars):
        if len(bar.lengths) < 2:
            continue
        for length in sorted(set(bar.lengths), reverse=True):
            remaining = list(bar.lengths)
            remaining.remove(length)
            shorter = kerf.shortest_fitting_stock(remaining, stock_lengths_mm, kerf_mm)
            if shorter is None or shorter >= bar.stock_length_mm:
                continue
            target = _best_fit_target(bars, source, length, kerf_mm)
            if target is None:
                continue
            result = list(bars)
            result[source] = RawBar.of(shorter, remaining)
            receiver = bars[target]
            result[target] = RawBar.of(receiver.stock_length_mm, (*receiver.lengths, length))
            return result
    return None


def _best_fit_target(bars: Sequence[RawBar], source: int, length: int, kerf_mm: int) -> int | None:
    need = kerf.item_size(length, kerf_mm)
    best: tuple[int, int] | None = None
    for index, bar in enumerate(bars):
        if index == source:
            continue
        free = kerf.free_capacity(bar.lengths, bar.stock_length_mm, kerf_mm)
        if free >= need and (best is None or (free, index) < best):
            best = (free, index)
    return None if best is None else best[1]


# --- step 4 ----------------------------------------------------------------------


def classify_remnant(remnant_mm: int, piece_lengths_mm: Sequence[int]) -> int | None:
    """Longest piece length of the size that fits the remnant (reusable),
    or ``None`` if nothing fits (waste). Remnants already exclude the last
    kerf (D3), so the comparison is a plain ``piece <= remnant``."""
    fitting = [length for length in piece_lengths_mm if length <= remnant_mm]
    return max(fitting) if fitting else None
