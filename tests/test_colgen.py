from __future__ import annotations

import itertools
from collections import Counter

import pytest
from benchmarks.generate import PRESETS, make_pieces
from hypothesis import given, settings
from hypothesis import strategies as st

from rebar_optimizer.domain.errors import Cancelled, IssueCode
from rebar_optimizer.domain.models import SolveStatus
from rebar_optimizer.domain.settings import OptimizerSettings
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import CancelToken
from rebar_optimizer.solver import colgen, kerf
from rebar_optimizer.solver.base import cut_counts
from rebar_optimizer.solver.colgen import price, solve_colgen
from rebar_optimizer.solver.ilp import IlpOptions, solve_patterns
from rebar_optimizer.solver.patterns import enumerate_patterns
from tests.test_ilp import greedy_total

# --- pricing knapsack ------------------------------------------------------------


@settings(max_examples=150, deadline=None)
@given(
    items=st.lists(
        st.tuples(st.floats(-1, 10, allow_nan=False), st.integers(1, 40), st.integers(1, 4)),
        min_size=1,
        max_size=4,
    ),
    capacity=st.integers(1, 120),
)
def test_price_matches_brute_force(items: list[tuple[float, int, int]], capacity: int) -> None:
    duals = [v for v, _, _ in items]
    weights = [w for _, w, _ in items]
    caps = [c for _, _, c in items]
    best = max(
        sum(d * a for d, a in zip(duals, counts, strict=True))
        for counts in itertools.product(*(range(c + 1) for c in caps))
        if sum(w * a for w, a in zip(weights, counts, strict=True)) <= capacity
    )
    result = price(duals, weights, caps, capacity)
    assert result.value == pytest.approx(best, abs=1e-9)
    assert all(0 <= a <= c for a, c in zip(result.counts, caps, strict=True))
    assert sum(w * a for w, a in zip(weights, result.counts, strict=True)) <= capacity


def test_price_uses_kerf_through_weights() -> None:
    # 3 x 4000 fits 12000 without kerf, not with kerf 3 (needs 12006).
    with_kerf = price([1.0], [kerf.item_size(4000, 3)], [5], kerf.capacity(12000, 3))
    without = price([1.0], [kerf.item_size(4000, 0)], [5], kerf.capacity(12000, 0))
    assert with_kerf.counts == (2,)
    assert without.counts == (3,)


def test_price_respects_demand_cap() -> None:
    assert price([1.0], [1000], [2], 12000).counts == (2,)


# --- solve_colgen against the exact ILP on enumerable problems ---------------------------


def exact_optimum(demand: Counter[int], stocks: tuple[int, ...], k: int) -> int:
    pattern_set = enumerate_patterns(demand, stocks, k, max_patterns=100_000)
    assert pattern_set is not None
    result = solve_patterns(demand, pattern_set, IlpOptions(time_limit_s=20))
    assert result is not None and result.status is SolveStatus.OPTIMAL
    assert result.lower_bound_mm is not None
    return result.lower_bound_mm


@settings(max_examples=15, deadline=None)
@given(
    demand=st.dictionaries(
        st.integers(5, 70).map(lambda x: x * 100), st.integers(1, 12), min_size=2, max_size=7
    ),
    k=st.sampled_from([0, 3]),
)
def test_colgen_close_to_optimal_with_valid_bound(demand: dict[int, int], k: int) -> None:
    stocks = (10000, 12000)
    counter = Counter(demand)
    optimum = exact_optimum(counter, stocks, k)
    result = solve_colgen(counter, stocks, k, IlpOptions(time_limit_s=10))
    assert result is not None
    assert result.status is SolveStatus.HEURISTIC
    assert all(cut_counts(result.bars)[n] >= q for n, q in demand.items())
    assert result.lower_bound_mm is not None
    assert result.lower_bound_mm <= optimum
    # PLAN 5.5 test: not worse than the optimum by more than one shortest stock bar
    total = sum(b.stock_length_mm for b in result.bars)
    assert total <= optimum + min(stocks)


def test_generated_columns_respect_kerf_and_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    original = colgen.solve_patterns

    def spy(demand, pattern_set, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(pattern_set)
        return original(demand, pattern_set, *args, **kwargs)

    monkeypatch.setattr(colgen, "solve_patterns", spy)
    demand = Counter({4000: 5, 2900: 7, 1950: 9, 1200: 4})
    solve_colgen(demand, (10000, 12000), 7, IlpOptions(time_limit_s=5))
    (pattern_set,) = seen
    assert len(pattern_set.patterns) > len(pattern_set.lengths)  # columns were generated
    for pattern in pattern_set.patterns:
        pieces = pattern.lengths(pattern_set.lengths)
        assert kerf.fits(pieces, pattern.stock_length_mm, 7)
        assert all(
            n <= demand[length]
            for length, n in zip(pattern_set.lengths, pattern.counts, strict=True)
        )


def test_farley_bound_is_valid_when_stopped_early(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(colgen, "MAX_ITERATIONS", 1)
    demand = Counter({3300: 9, 2700: 11, 1800: 6, 900: 14})
    optimum = exact_optimum(demand, (10000, 12000), 0)
    result = solve_colgen(demand, (10000, 12000), 0, IlpOptions(time_limit_s=5))
    assert result is not None and result.lower_bound_mm is not None
    assert 0 < result.lower_bound_mm <= optimum


def test_colgen_cancel() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        solve_colgen(Counter({1000: 3}), (12000,), 0, IlpOptions(1), cancel=token)


# --- planner integration ---------------------------------------------------------------


def test_planner_falls_back_to_colgen_over_the_pattern_limit() -> None:
    pieces = [p for p in make_pieces(PRESETS["medium"], seed=0) if p.size == "DB25"]
    settings_ = OptimizerSettings(
        stock_lengths_mm=(10000, 12000), max_patterns=100, time_limit_s=10
    )
    result = planner.plan(pieces, settings_)  # verify_plan runs inside
    size_plan = result.sizes[0]
    assert size_plan.status in (SolveStatus.HEURISTIC, SolveStatus.OPTIMAL)
    assert [i.code for i in result.issues] == [IssueCode.PATTERN_LIMIT_EXCEEDED]
    exact = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(10000, 12000))).sizes[0]
    assert size_plan.lower_bound_mm is not None
    assert size_plan.lower_bound_mm <= exact.total_stock_mm
    assert size_plan.total_stock_mm <= exact.total_stock_mm + 10000


def test_large_size_beats_or_matches_greedy() -> None:
    pieces = [p for p in make_pieces(PRESETS["large"], seed=0) if p.size == "DB16"]
    settings_ = OptimizerSettings(stock_lengths_mm=(10000, 12000), time_limit_s=10)
    size_plan = planner.plan(pieces, settings_).sizes[0]
    assert size_plan.total_stock_mm <= greedy_total(pieces, (10000, 12000), 0)
    assert size_plan.gap is not None and size_plan.gap < 0.01
