from __future__ import annotations

import random
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rebar_optimizer.domain.errors import Cancelled
from rebar_optimizer.domain.models import StockSource
from rebar_optimizer.services.cancel import CancelToken
from rebar_optimizer.services.postprocess import (
    classify_remnant,
    downsize_all,
    postprocess,
    remove_overcuts,
    reuse_remnants,
)
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import RawBar, cut_counts, total_stock_mm
from rebar_optimizer.solver.greedy import GreedySolver


def bars(*specs: tuple[int, tuple[int, ...]]) -> list[RawBar]:
    return [RawBar.of(stock, lengths) for stock, lengths in specs]


# --- step 1: over-cut removal ----------------------------------------------------


def test_overcut_removed_where_stock_shrinks_most() -> None:
    stocks = (6000, 10000, 12000)
    plan = bars((10000, (5000, 4500)), (12000, (5000, 6100)))
    demand = {5000: 1, 4500: 1, 6100: 1}
    # Removing from bar 1 leaves the longer remnant (5900 vs 5500), but bar 0 then
    # shrinks 10 m -> 6 m (saves 4 m) whereas bar 1 only 12 m -> 10 m (saves 2 m).
    result = downsize_all(remove_overcuts(plan, demand, stocks, 0), stocks, 0)
    assert cut_counts(result) == Counter(demand)
    assert total_stock_mm(result) == 6000 + 12000


def test_overcut_removed_to_concentrate_remnant_when_nothing_shrinks() -> None:
    plan = bars((12000, (5000, 5000, 1500)), (12000, (5000, 6000)))
    demand = {5000: 2, 1500: 1, 6000: 1}
    result = remove_overcuts(plan, demand, (12000,), 0)
    assert sorted(b.lengths for b in result) == [(5000, 5000, 1500), (6000,)]


def test_overcut_bar_that_empties_is_dropped() -> None:
    plan = bars((12000, (6000, 6000)), (10000, (4000,)))
    result = remove_overcuts(plan, {6000: 2}, (10000, 12000), 0)
    assert result == bars((12000, (6000, 6000)))


def reference_remove_overcuts(
    plan: list[RawBar], demand: dict[int, int], stocks: tuple[int, ...], kerf_mm: int
) -> list[RawBar]:
    """The plain definition: score every bar for every surplus piece, first best wins."""
    result: list[RawBar | None] = list(plan)
    surplus = cut_counts(plan) - Counter(demand)
    for length in sorted(surplus, reverse=True):
        for _ in range(surplus[length]):
            best: tuple[tuple[int, int, int], int] | None = None
            for index, bar in enumerate(result):
                if bar is None or length not in bar.lengths:
                    continue
                remaining = list(bar.lengths)
                remaining.remove(length)
                if not remaining:
                    saving, slack = bar.stock_length_mm, 0
                else:
                    shorter = (
                        None
                        if bar.is_inventory
                        else kerf.shortest_fitting_stock(remaining, stocks, kerf_mm)
                    )
                    saving = max(0, bar.stock_length_mm - (shorter or bar.stock_length_mm))
                    slack = kerf.slack_mm(remaining, bar.stock_length_mm, kerf_mm)
                score = (0, saving, slack) if bar.is_inventory else (saving, 0, slack)
                if best is None or score > best[0]:
                    best = (score, index)
            assert best is not None
            bar = result[best[1]]
            assert bar is not None
            remaining = list(bar.lengths)
            remaining.remove(length)
            result[best[1]] = (
                RawBar.of(bar.stock_length_mm, remaining, bar.source) if remaining else None
            )
    return [bar for bar in result if bar is not None]


@pytest.mark.parametrize("seed", range(40))
def test_overcut_removal_matches_the_plain_definition(seed: int) -> None:
    rng = random.Random(seed)
    stocks = (6000, 10000, 12000)
    lengths = [rng.randrange(500, 6001, 500) for _ in range(rng.randint(2, 6))]
    kerf_mm = rng.choice((0, 3))
    plan: list[RawBar] = []
    for _ in range(rng.randint(1, 60)):  # few distinct contents, repeated as in ILP results
        cuts: list[int] = []
        while True:
            length = rng.choice(lengths)
            if not kerf.fits([*cuts, length], 12000, kerf_mm):
                break
            cuts.append(length)
        source = StockSource.INVENTORY if rng.random() < 0.2 else StockSource.NEW
        plan.extend([RawBar.of(12000, cuts, source)] * rng.randint(1, 4))
    counts = cut_counts(plan)
    demand = {length: rng.randint(0, n) for length, n in counts.items()}
    assert remove_overcuts(plan, demand, stocks, kerf_mm) == reference_remove_overcuts(
        plan, demand, stocks, kerf_mm
    )


# --- step 2: downsizing ----------------------------------------------------------


def test_downsize_all_picks_shortest_fitting_stock() -> None:
    result = downsize_all(bars((12000, (3000, 2000)), (12000, (9000,))), (6000, 10000, 12000), 0)
    assert [b.stock_length_mm for b in result] == [6000, 10000]


# --- step 3: remnant reuse -----------------------------------------------------------


def test_remnants_absorb_a_whole_bar() -> None:
    plan = bars((12000, (6000, 3000)), (12000, (6000, 3000)), (12000, (3000, 3000)))
    result = reuse_remnants(plan, (12000,), 0)
    assert total_stock_mm(result) == 24000
    assert cut_counts(result) == cut_counts(plan)
    assert all(kerf.fits(b.lengths, b.stock_length_mm, 0) for b in result)


def test_moving_one_piece_lets_a_bar_shrink() -> None:
    plan = bars((12000, (5000, 2000)), (12000, (6000, 1000)))
    result = reuse_remnants(plan, (6000, 12000), 0)
    assert total_stock_mm(result) == 18000
    assert cut_counts(result) == cut_counts(plan)


def test_remnant_reuse_respects_kerf() -> None:
    # Bar A = 6000 + 2997 with k=3 leaves remnant 12000 - 8997 - 3 - 3 = 2997.
    fits_exactly = bars((12000, (6000, 2997)), (12000, (2997,)))
    assert total_stock_mm(reuse_remnants(fits_exactly, (12000,), 3)) == 12000
    one_mm_too_long = bars((12000, (6000, 2997)), (12000, (2998,)))
    assert total_stock_mm(reuse_remnants(one_mm_too_long, (12000,), 3)) == 24000


def test_reuse_is_cancellable() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        reuse_remnants(bars((12000, (1000,))), (12000,), 0, token)


# --- step 4: classification ------------------------------------------------------


def test_remnant_equal_to_shortest_piece_is_reusable() -> None:
    assert classify_remnant(1200, (1200, 2100, 3500)) == 1200


def test_remnant_one_mm_short_is_waste() -> None:
    assert classify_remnant(1199, (1200, 2100, 3500)) is None


def test_longest_cuttable_piece_is_reported() -> None:
    assert classify_remnant(3000, (1200, 2100, 3500)) == 2100


# --- properties ------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    demand=st.dictionaries(st.integers(300, 6000), st.integers(1, 6), min_size=1, max_size=6),
    stocks=st.sets(st.sampled_from([6000, 9000, 10000, 12000]), min_size=1),
    k=st.sampled_from([0, 3, 10]),
    extra=st.lists(st.integers(0, 5), max_size=3),
)
def test_postprocess_never_adds_length_and_keeps_demand(
    demand: dict[int, int], stocks: set[int], k: int, extra: list[int]
) -> None:
    stock_set = tuple(sorted(stocks))
    if max(demand) > stock_set[-1]:
        return
    # Simulate ILP over-cutting: solve for a bigger demand, then post-process to the real one.
    inflated = Counter(demand)
    for i, length in zip(extra, sorted(demand), strict=False):
        inflated[length] += i
    start = list(GreedySolver().solve(inflated, stock_set, k).bars)
    result = postprocess(start, demand, stock_set, k)
    assert total_stock_mm(result) <= total_stock_mm(start)
    assert cut_counts(result) == Counter(demand)
    assert all(b.stock_length_mm in stock_set for b in result)
    assert all(kerf.fits(b.lengths, b.stock_length_mm, k) for b in result)


def test_reuse_remnants_scales_to_large_sizes() -> None:
    """A 100,000-row input stalled for hours in this step (old loop was ~B² per move)."""
    import random
    import time

    rng = random.Random(1)
    demand = Counter(rng.randrange(300, 11900, 10) for _ in range(20_000))
    stocks = (10000, 12000)
    start = downsize_all(GreedySolver().solve(demand, stocks, 0).bars, stocks, 0)
    began = time.perf_counter()
    result = reuse_remnants(start, stocks, 0)
    elapsed = time.perf_counter() - began
    assert elapsed < 5, f"step 3 took {elapsed:.1f}s for {len(start)} bars"
    assert cut_counts(result) == cut_counts(start)
    assert total_stock_mm(result) <= total_stock_mm(start)
    # A fixpoint (D5): running the step again finds no further improving move.
    assert total_stock_mm(reuse_remnants(result, stocks, 0)) == total_stock_mm(result)
