"""Post-processing of a length-level plan, in the fixed order of D5.

1. remove over-cut pieces (ILP uses ``>=`` demand constraints)
2. downsize each bar to the shortest allowed stock that still holds it; with
   inventory (phase 8) this is ``assign_stock``, which also moves bars onto
   unused inventory bars when that saves purchased stock
3. reuse remnants: move pieces into other bars' remnants when that closes or
   downsizes the source bar (only strictly improving moves; repeat to a fixpoint)

Step 4 (classifying remnants) is ``classify_remnant``, applied when the plan is
summarized. No step ever makes ``stock_cost`` (purchased length, then inventory
length; the plain total without inventory) worse.
"""

from __future__ import annotations

import bisect
import time
from collections import Counter
from collections.abc import Sequence

from rebar_optimizer.domain.models import StockSource
from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import (
    NO_INVENTORY,
    Demand,
    Inventory,
    RawBar,
    cut_counts,
    expand_inventory,
    stock_cost,
)


def postprocess(
    bars: Sequence[RawBar],
    demand: Demand,
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    cancel: CancelToken | None = None,
    *,
    inventory: Inventory = NO_INVENTORY,
    deadline: float | None = None,
) -> list[RawBar]:
    """D5 steps 1-3. Step 3 stops improving at ``deadline`` (``time.monotonic()``)."""
    before = stock_cost(bars)
    result = remove_overcuts(bars, demand, stock_lengths_mm, kerf_mm, cancel)
    result = assign_stock(result, stock_lengths_mm, kerf_mm, inventory)
    result = reuse_remnants(result, stock_lengths_mm, kerf_mm, cancel, deadline=deadline)
    result = assign_stock(result, stock_lengths_mm, kerf_mm, inventory)
    if stock_cost(result) > before:  # pragma: no cover - guarded by construction
        raise AssertionError("post-processing increased the stock cost")
    return result


# --- step 1 ----------------------------------------------------------------------


def remove_overcuts(
    bars: Sequence[RawBar],
    demand: Demand,
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    cancel: CancelToken | None = None,
) -> list[RawBar]:
    """Drop surplus pieces (D5 step 1): prefer (a) the bar whose stock can then
    shrink the most (vanishing counts as shrinking to 0; purchased stock before
    inventory), else (b) the bar that ends up with the longest remnant, so
    remnants concentrate in one bar.

    Bars are indexed by the lengths they hold: scanning every bar for every
    surplus piece took seconds on 50,000-bar ILP results, without a cancel check.
    """
    result: list[RawBar | None] = list(bars)
    surplus = cut_counts(bars) - Counter(demand)
    holders: dict[int, set[int]] = {length: set() for length in surplus}
    for position, original in enumerate(bars):
        for length in set(original.lengths) & holders.keys():
            holders[length].add(position)
    removed = 0
    for length in sorted(surplus, reverse=True):
        for _ in range(surplus[length]):
            removed += 1
            if removed % CANCEL_CHECK_EVERY == 0:
                check_cancelled(cancel)
            candidates = sorted(holders[length])
            live = [bar_ for i in candidates if (bar_ := result[i]) is not None]
            index = candidates[_bar_to_remove_from(live, length, stock_lengths_mm, kerf_mm)]
            bar = result[index]
            assert bar is not None
            remaining = list(bar.lengths)
            remaining.remove(length)
            if length not in remaining:
                holders[length].discard(index)
            if remaining:
                result[index] = RawBar.of(bar.stock_length_mm, remaining, bar.source)
            else:
                result[index] = None
                for other in holders.values():
                    other.discard(index)
    return [bar for bar in result if bar is not None]


def _bar_to_remove_from(
    bars: Sequence[RawBar], length: int, stock_lengths_mm: Sequence[int], kerf_mm: int
) -> int:
    best_index = -1
    best_score: tuple[int, int, int] = (-1, -1, -1)
    for index, bar in enumerate(bars):
        if length not in bar.lengths:
            continue
        remaining = list(bar.lengths)
        remaining.remove(length)
        if not remaining:
            saving = bar.stock_length_mm
            slack = 0
        else:
            # An inventory bar never shrinks here; ``assign_stock`` may swap it later.
            shorter = (
                None
                if bar.is_inventory
                else kerf.shortest_fitting_stock(remaining, stock_lengths_mm, kerf_mm)
            )
            saving = max(0, bar.stock_length_mm - (shorter or bar.stock_length_mm))
            slack = kerf.slack_mm(remaining, bar.stock_length_mm, kerf_mm)
        score = (0, saving, slack) if bar.is_inventory else (saving, 0, slack)
        if score > best_score:
            best_index, best_score = index, score
    if best_index < 0:  # pragma: no cover - surplus implies presence
        raise AssertionError(f"no bar holds surplus length {length}")
    return best_index


# --- step 2 ----------------------------------------------------------------------


def downsize_all(
    bars: Sequence[RawBar], stock_lengths_mm: Sequence[int], kerf_mm: int
) -> list[RawBar]:
    """Each new bar on the shortest new stock that holds it; inventory bars unchanged."""
    result: list[RawBar] = []
    for bar in bars:
        if bar.is_inventory:
            result.append(bar)
            continue
        stock = kerf.shortest_fitting_stock(bar.lengths, stock_lengths_mm, kerf_mm)
        if stock is None:
            raise ValueError(f"bar {bar} fits no allowed stock length")
        result.append(RawBar(stock, bar.lengths))
    return result


def assign_stock(
    bars: Sequence[RawBar],
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    inventory: Inventory = NO_INVENTORY,
) -> list[RawBar]:
    """Choose the stock of every bar (D5 step 2), keeping the pieces as they are.

    Without inventory this is ``downsize_all``. With inventory, bars are taken
    from the largest need down and each gets the shortest unused inventory bar
    that holds it, else the shortest new stock. A bar needing more stock saves
    at least as much purchase, and every later bar fits what an earlier one
    left, so this maximizes the purchase saved. The result is kept only if its
    ``stock_cost`` is no worse than plain downsizing.
    """
    baseline = downsize_all(bars, stock_lengths_mm, kerf_mm)
    pool = expand_inventory(inventory)
    if not pool:
        return baseline
    order = sorted(
        range(len(bars)),
        key=lambda i: (kerf.free_capacity(bars[i].lengths, 0, kerf_mm), i),
    )
    assigned: list[RawBar] = []
    for i in order:
        lengths = bars[i].lengths
        slot = next((j for j, stock in enumerate(pool) if kerf.fits(lengths, stock, kerf_mm)), None)
        if slot is not None:
            assigned.append(RawBar(pool.pop(slot), lengths, StockSource.INVENTORY))
            continue
        stock = kerf.shortest_fitting_stock(lengths, stock_lengths_mm, kerf_mm)
        if stock is None:  # pragma: no cover - the input assignment shows one exists
            return baseline
        assigned.append(RawBar(stock, lengths))
    return assigned if stock_cost(assigned) <= stock_cost(baseline) else baseline


# --- step 3 ----------------------------------------------------------------------

CANCEL_CHECK_EVERY = 256
"""Bars examined between cancel checks inside one pass of step 3."""


def reuse_remnants(
    bars: Sequence[RawBar],
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    cancel: CancelToken | None = None,
    *,
    deadline: float | None = None,
) -> list[RawBar]:
    """Move pieces into other bars' remnants while that closes or downsizes a bar.

    Stops early once ``deadline`` (``time.monotonic()``) passes, keeping the
    moves made so far: each of them was an improvement, so the plan is valid
    and never worse than the input (the user agreed to this D5 change so that
    every size stays within its time limit).

    Each pass tries to empty bars (purchased first, longest first), then to
    move single pieces so a purchased bar fits a shorter stock. Only strictly
    improving moves are applied, and passes repeat until one changes nothing
    (D5 step 3). Target bars are looked up by bisection in a list ordered by
    free capacity, so a pass costs about ``B log B`` rather than ``B²``: a
    100,000-piece size took hours with the old restart-after-every-move loop.
    """
    pool = _BarPool(bars, kerf_mm, deadline)
    while True:
        check_cancelled(cancel)
        if pool.out_of_time():
            return pool.bars()
        closed = pool.close_pass(cancel)
        shrunk = pool.downsize_pass(stock_lengths_mm, cancel)
        if not (closed or shrunk):
            return pool.bars()


class _BarPool:
    """Bars plus their free capacities, indexed by ``(free, index)`` for best fit.

    ``free`` is the knapsack capacity left (``kerf.free_capacity``): a piece
    ``l`` fits bar ``i`` iff ``l + k <= free[i]``. Closed bars become ``None``.
    """

    def __init__(self, bars: Sequence[RawBar], kerf_mm: int, deadline: float | None = None) -> None:
        self.kerf_mm = kerf_mm
        self._deadline = deadline
        self._bars: list[RawBar | None] = list(bars)
        self._free = [kerf.free_capacity(b.lengths, b.stock_length_mm, kerf_mm) for b in bars]
        self._by_free = sorted((free, i) for i, free in enumerate(self._free))
        self._total_free = sum(self._free)

    def bars(self) -> list[RawBar]:
        return [bar for bar in self._bars if bar is not None]

    def out_of_time(self) -> bool:
        return self._deadline is not None and time.monotonic() > self._deadline

    # --- index maintenance ---------------------------------------------------------

    def _unindex(self, i: int) -> None:
        pos = bisect.bisect_left(self._by_free, (self._free[i], i))
        del self._by_free[pos]
        self._total_free -= self._free[i]

    def _index(self, i: int) -> None:
        bisect.insort(self._by_free, (self._free[i], i))
        self._total_free += self._free[i]

    def _replace(self, i: int, bar: RawBar) -> None:
        self._unindex(i)
        self._bars[i] = bar
        self._free[i] = kerf.free_capacity(bar.lengths, bar.stock_length_mm, self.kerf_mm)
        self._index(i)

    def _best_fit(self, need: int, exclude: int) -> int | None:
        """Bar with the least free capacity that still takes ``need`` (lowest index on ties)."""
        pos = bisect.bisect_left(self._by_free, (need, -1))
        while pos < len(self._by_free):
            index = self._by_free[pos][1]
            if index != exclude:
                return index
            pos += 1
        return None

    # --- moves -------------------------------------------------------------------------

    def close_pass(self, cancel: CancelToken | None) -> bool:
        """Empty every bar whose pieces all fit the others' remnants."""
        live = [i for i, bar in enumerate(self._bars) if bar is not None]
        order = sorted(live, key=self._close_key)
        changed = False
        for step, source in enumerate(order):
            if step % CANCEL_CHECK_EVERY == 0:
                check_cancelled(cancel)
                if self.out_of_time():
                    return changed
            if self._bars[source] is not None and self._try_close(source):
                changed = True
        return changed

    def _close_key(self, i: int) -> tuple[bool, int, int, int]:
        bar = self._bars[i]
        assert bar is not None
        # Close purchased bars before inventory bars, longest first, fewest pieces first.
        return (bar.is_inventory, -bar.stock_length_mm, len(bar.lengths), i)

    def _try_close(self, source: int) -> bool:
        bar = self._bars[source]
        assert bar is not None
        needs = [kerf.item_size(length, self.kerf_mm) for length in bar.lengths]
        # Cheap rejections before simulating the placement.
        if sum(needs) > self._total_free - self._free[source]:
            return False
        largest_other = max((free for free, i in self._by_free[-2:] if i != source), default=-1)
        if largest_other < max(needs):
            return False

        self._unindex(source)
        trial: dict[int, int] = {}  # target -> free capacity during the trial
        added: dict[int, list[int]] = {}
        placed = True
        for length, need in zip(bar.lengths, needs, strict=True):  # longest first
            pos = bisect.bisect_left(self._by_free, (need, -1))
            if pos == len(self._by_free):
                placed = False
                break
            free, target = self._by_free.pop(pos)
            trial[target] = free - need
            bisect.insort(self._by_free, (free - need, target))
            added.setdefault(target, []).append(length)
        if not placed:
            # Undo the trial and put the source back.
            for target, free in trial.items():
                pos = bisect.bisect_left(self._by_free, (free, target))
                del self._by_free[pos]
                bisect.insort(self._by_free, (self._free[target], target))
            self._index(source)
            return False
        for target, pieces in added.items():
            receiver = self._bars[target]
            assert receiver is not None
            self._bars[target] = RawBar.of(
                receiver.stock_length_mm, receiver.lengths + tuple(pieces), receiver.source
            )
            self._total_free -= self._free[target] - trial[target]
            self._free[target] = trial[target]
        self._bars[source] = None
        return True

    def downsize_pass(self, stock_lengths_mm: Sequence[int], cancel: CancelToken | None) -> bool:
        """Move one piece out of purchased bars so they fit a shorter stock.

        Inventory bars are left to ``assign_stock``.
        """
        changed = False
        for source in range(len(self._bars)):
            if source % CANCEL_CHECK_EVERY == 0:
                check_cancelled(cancel)
                if self.out_of_time():
                    return changed
            bar = self._bars[source]
            if bar is None or len(bar.lengths) < 2 or bar.is_inventory:
                continue
            for length in sorted(set(bar.lengths), reverse=True):
                remaining = list(bar.lengths)
                remaining.remove(length)
                shorter = kerf.shortest_fitting_stock(remaining, stock_lengths_mm, self.kerf_mm)
                if shorter is None or shorter >= bar.stock_length_mm:
                    continue
                target = self._best_fit(kerf.item_size(length, self.kerf_mm), exclude=source)
                if target is None:
                    continue
                receiver = self._bars[target]
                assert receiver is not None
                self._replace(
                    target,
                    RawBar.of(
                        receiver.stock_length_mm, (*receiver.lengths, length), receiver.source
                    ),
                )
                self._replace(source, RawBar.of(shorter, remaining))
                changed = True
                break  # this bar changed; it is looked at again in the next pass
        return changed


# --- step 4 ----------------------------------------------------------------------


def classify_remnant(remnant_mm: int, piece_lengths_mm: Sequence[int]) -> int | None:
    """Longest piece length of the size that fits the remnant (reusable),
    or ``None`` if nothing fits (waste). Remnants already exclude the last
    kerf (D3), so the comparison is a plain ``piece <= remnant``.

    ``piece_lengths_mm`` must be sorted shortest first (``SizePlan.piece_lengths_mm`` is).
    """
    pos = bisect.bisect_right(piece_lengths_mm, remnant_mm)
    return piece_lengths_mm[pos - 1] if pos else None
