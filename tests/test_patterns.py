from __future__ import annotations

import itertools
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rebar_optimizer.domain.errors import Cancelled
from rebar_optimizer.services.cancel import CancelToken
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import RawBar
from rebar_optimizer.solver.patterns import (
    Pattern,
    enumerate_patterns,
    iter_maximal_patterns,
    pattern_of,
)


def brute_force(demand: dict[int, int], stocks: list[int], k: int) -> set[Pattern]:
    """Every count vector, filtered by the definition in PLAN.md 5.3."""
    lengths = sorted(demand, reverse=True)
    stocks = sorted(stocks)
    result: set[Pattern] = set()
    for counts in itertools.product(*(range(demand[length] + 1) for length in lengths)):
        pieces = [length for length, n in zip(lengths, counts, strict=True) for _ in range(n)]
        if not pieces:
            continue
        for i, stock in enumerate(stocks):
            if not kerf.fits(pieces, stock, k):
                continue
            if i and kerf.fits(pieces, stocks[i - 1], k):
                continue  # dominated: same pieces fit a shorter stock
            maximal = all(
                n >= demand[length] or not kerf.fits([*pieces, length], stock, k)
                for length, n in zip(lengths, counts, strict=True)
            )
            if maximal:
                result.add(Pattern(stock, counts))
    return result


@settings(max_examples=60, deadline=None)
@given(
    demand=st.dictionaries(
        st.integers(500, 7000).map(lambda x: x - x % 100), st.integers(1, 4), min_size=1, max_size=4
    ),
    stocks=st.sets(st.sampled_from([6000, 9000, 10000, 12000]), min_size=1, max_size=3),
    k=st.sampled_from([0, 3, 50]),
)
def test_matches_brute_force(demand: dict[int, int], stocks: set[int], k: int) -> None:
    found = list(iter_maximal_patterns(demand, sorted(stocks), k))
    assert len(found) == len(set(found)), "patterns must be unique"
    assert set(found) == brute_force(demand, sorted(stocks), k)


def test_simple_example() -> None:
    # 12 m bar: 5+5 (+ nothing else fits: 5+5+3 > 12), 5+3+3, 3x3 ... capped by demand
    patterns = set(iter_maximal_patterns({5000: 2, 3000: 3}, [12000], 0))
    assert patterns == {Pattern(12000, (2, 0)), Pattern(12000, (1, 2)), Pattern(12000, (0, 3))}


def test_dominated_patterns_move_to_the_shorter_stock() -> None:
    patterns = set(iter_maximal_patterns({5000: 1}, [6000, 12000], 0))
    assert patterns == {Pattern(6000, (1,))}


def test_limit_returns_none() -> None:
    demand = {length: 3 for length in range(500, 3000, 100)}
    assert enumerate_patterns(demand, [12000], 0, max_patterns=10) is None
    assert enumerate_patterns({1000: 1}, [12000], 0, max_patterns=10) is not None


def test_extra_bars_are_added_even_when_not_maximal() -> None:
    demand = {5000: 2, 3000: 3}
    greedy_bar = RawBar.of(12000, [5000])  # not maximal
    result = enumerate_patterns(demand, [12000], 0, max_patterns=100, extra_bars=[greedy_bar])
    assert result is not None
    assert pattern_of(greedy_bar, result.lengths) in result.patterns
    assert result.to_raw_bar(pattern_of(greedy_bar, result.lengths)) == greedy_bar


def test_patterns_respect_demand_and_kerf() -> None:
    demand = Counter({4000: 2, 2000: 5, 1500: 1})
    result = enumerate_patterns(demand, [10000, 12000], 5, max_patterns=1000)
    assert result is not None
    for pattern in result.patterns:
        pieces = pattern.lengths(result.lengths)
        assert kerf.fits(pieces, pattern.stock_length_mm, 5)
        assert all(
            n <= demand[length] for length, n in zip(result.lengths, pattern.counts, strict=True)
        )


def test_enumeration_is_cancellable() -> None:
    token = CancelToken()
    token.cancel()
    demand = {length: 5 for length in range(300, 4000, 50)}
    with pytest.raises(Cancelled):
        list(iter_maximal_patterns(demand, [12000], 0, token))
