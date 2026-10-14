from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rebar_optimizer.solver import kerf


def test_exact_fill_needs_no_last_cut() -> None:
    usage = kerf.bar_usage([5000, 4997], 10000, 3)  # r == 0
    assert (usage.cut_count, usage.kerf_loss_mm, usage.remnant_mm) == (1, 3, 0)


def test_tiny_slack_is_eaten_by_the_last_cut() -> None:
    usage = kerf.bar_usage([5000, 4996], 10000, 3)  # 0 < r = 1 <= k
    assert (usage.cut_count, usage.kerf_loss_mm, usage.remnant_mm) == (2, 4, 0)


def test_large_slack_leaves_a_remnant() -> None:
    usage = kerf.bar_usage([5000, 4000], 10000, 3)  # r = 997 > k
    assert (usage.cut_count, usage.kerf_loss_mm, usage.remnant_mm) == (2, 6, 994)


@pytest.mark.parametrize(
    ("lengths", "expected"),
    [([5000, 5000], (1, 0, 0)), ([5000, 4000], (2, 0, 1000)), ([10000], (0, 0, 0))],
)
def test_zero_kerf(lengths: list[int], expected: tuple[int, int, int]) -> None:
    usage = kerf.bar_usage(lengths, 10000, 0)
    assert (usage.cut_count, usage.kerf_loss_mm, usage.remnant_mm) == expected


def test_bar_usage_rejects_overfull_and_empty_bars() -> None:
    with pytest.raises(ValueError, match="do not fit"):
        kerf.bar_usage([5000, 4998], 10000, 3)
    with pytest.raises(ValueError, match="at least one"):
        kerf.bar_usage([], 10000, 3)


def test_shortest_fitting_stock() -> None:
    assert kerf.shortest_fitting_stock([4000, 3000], [6000, 8000, 12000], 0) == 8000
    assert kerf.shortest_fitting_stock([4000, 3998], [8000, 12000], 3) == 12000
    assert kerf.shortest_fitting_stock([13000], [6000, 12000], 0) is None


lengths_strategy = st.lists(st.integers(1, 6000), min_size=1, max_size=8)


@given(lengths_strategy, st.integers(1, 15000), st.integers(0, 20))
def test_both_feasibility_forms_agree(lengths: list[int], stock: int, k: int) -> None:
    knapsack = sum(kerf.item_size(x, k) for x in lengths) <= kerf.capacity(stock, k)
    assert kerf.fits(lengths, stock, k) == knapsack
    assert (kerf.free_capacity(lengths, stock, k) >= 0) == knapsack


@given(lengths_strategy, st.integers(1, 15000), st.integers(0, 20))
def test_accounting_adds_up(lengths: list[int], stock: int, k: int) -> None:
    if not kerf.fits(lengths, stock, k):
        return
    usage = kerf.bar_usage(lengths, stock, k)
    assert usage.pieces_mm + usage.kerf_loss_mm + usage.remnant_mm == stock
    assert usage.cut_count in (len(lengths) - 1, len(lengths))
    assert usage.remnant_mm >= 0
    assert usage.kerf_loss_mm >= k * (len(lengths) - 1)


@given(lengths_strategy, st.integers(1, 15000), st.integers(0, 20), st.integers(1, 6000))
def test_remnant_holds_piece_iff_piece_fits(
    lengths: list[int], stock: int, k: int, extra: int
) -> None:
    """'remnant >= l' (D5) is the same test as 'the bar still fits l' (D3)."""
    if not kerf.fits(lengths, stock, k):
        return
    remnant = kerf.bar_usage(lengths, stock, k).remnant_mm
    assert (extra <= remnant) == kerf.fits([*lengths, extra], stock, k)
