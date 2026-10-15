from __future__ import annotations

import threading
import time
from collections import Counter
from functools import cache

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from benchmarks.generate import PRESETS, make_pieces
from rebar_optimizer.domain.errors import Cancelled
from rebar_optimizer.domain.models import Piece, SolveStatus
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import CancelToken
from rebar_optimizer.services.postprocess import postprocess
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import cut_counts, total_stock_mm
from rebar_optimizer.solver.greedy import GreedySolver
from rebar_optimizer.solver.ilp import IlpOptions, solve_patterns
from rebar_optimizer.solver.patterns import enumerate_patterns


def brute_force_total(lengths: list[int], stocks: list[int], k: int) -> int:
    """Exact optimum by DP over subsets (fine for <= 8 pieces)."""
    n = len(lengths)
    full = (1 << n) - 1

    @cache
    def best(mask: int) -> int:
        if mask == 0:
            return 0
        lowest = mask & -mask
        result = 1 << 60
        sub = mask
        while sub:
            if sub & lowest:
                pieces = [lengths[i] for i in range(n) if sub >> i & 1]
                stock = kerf.shortest_fitting_stock(pieces, stocks, k)
                if stock is not None:
                    result = min(result, stock + best(mask ^ sub))
            sub = (sub - 1) & mask
        return result

    return best(full)


def as_pieces(rows: list[tuple[int, int]]) -> list[Piece]:
    return [Piece("DB12", str(i), length, qty, (i + 2,)) for i, (length, qty) in enumerate(rows)]


def greedy_total(pieces: list[Piece], stocks: tuple[int, ...], k: int) -> int:
    demand: Counter[int] = Counter()
    for p in pieces:
        demand[p.length_mm] += p.quantity
    bars = GreedySolver().solve(demand, stocks, k).bars
    return total_stock_mm(postprocess(bars, demand, stocks, k))


small_rows = st.lists(
    st.tuples(st.integers(5, 110).map(lambda x: x * 100), st.integers(1, 3)),
    min_size=1,
    max_size=4,
).filter(lambda rows: sum(q for _, q in rows) <= 8)


@settings(max_examples=50, deadline=None)
@given(
    rows=small_rows,
    stocks=st.sets(st.sampled_from([6000, 9000, 10000, 12000]), min_size=1, max_size=3),
    k=st.sampled_from([0, 3]),
)
def test_oracle_mixed_mode(rows: list[tuple[int, int]], stocks: set[int], k: int) -> None:
    pieces = as_pieces(rows)
    if max(p.length_mm for p in pieces) > max(stocks):
        return
    settings_ = OptimizerSettings(stock_lengths_mm=tuple(stocks), kerf_mm=k, time_limit_s=5)
    size_plan = planner.plan(pieces, settings_).sizes[0]
    lengths = [p.length_mm for p in pieces for _ in range(p.quantity)]
    assert size_plan.total_stock_mm == brute_force_total(lengths, sorted(stocks), k)
    assert size_plan.gap == 0


@settings(max_examples=30, deadline=None)
@given(rows=small_rows, k=st.sampled_from([0, 3]))
def test_oracle_single_mode(rows: list[tuple[int, int]], k: int) -> None:
    pieces = as_pieces(rows)
    stocks = [9000, 12000]
    if max(p.length_mm for p in pieces) > max(stocks):
        return
    settings_ = OptimizerSettings(
        stock_lengths_mm=tuple(stocks), stock_mode=StockMode.SINGLE, kerf_mm=k, time_limit_s=5
    )
    size_plan = planner.plan(pieces, settings_).sizes[0]
    lengths = [p.length_mm for p in pieces for _ in range(p.quantity)]
    expected = min(brute_force_total(lengths, [s], k) for s in stocks if s >= max(lengths))
    assert size_plan.total_stock_mm == expected


@settings(max_examples=25, deadline=None)
@given(
    rows=st.lists(
        st.tuples(st.integers(3, 80).map(lambda x: x * 100), st.integers(1, 8)),
        min_size=1,
        max_size=8,
    ),
    k=st.sampled_from([0, 3]),
)
def test_never_worse_than_greedy(rows: list[tuple[int, int]], k: int) -> None:
    pieces = as_pieces(rows)
    stocks = (10000, 12000)
    settings_ = OptimizerSettings(stock_lengths_mm=stocks, kerf_mm=k, time_limit_s=2)
    size_plan = planner.plan(pieces, settings_).sizes[0]
    assert size_plan.total_stock_mm <= greedy_total(pieces, stocks, k)
    assert size_plan.lower_bound_mm is not None
    assert size_plan.lower_bound_mm <= size_plan.total_stock_mm


def test_ilp_satisfies_demand_and_bound() -> None:
    demand = Counter({3500: 7, 2100: 9, 1200: 13, 5750: 3})
    pattern_set = enumerate_patterns(demand, (10000, 12000), 0, max_patterns=10_000)
    assert pattern_set is not None
    result = solve_patterns(demand, pattern_set, IlpOptions(time_limit_s=10))
    assert result is not None
    assert result.status is SolveStatus.OPTIMAL
    assert all(cut_counts(result.bars)[length] >= n for length, n in demand.items())
    assert result.lower_bound_mm == result.total_stock_mm


# --- time limits, determinism, cancel ----------------------------------------------


@pytest.fixture(scope="module")
def hard_size() -> list[Piece]:
    """The slowest size of the medium benchmark (several seconds to prove optimal)."""
    return [p for p in make_pieces(PRESETS["medium"], seed=2) if p.size == "RB9"]


def test_tiny_time_limit_still_returns_a_verified_plan(hard_size: list[Piece]) -> None:
    settings_ = OptimizerSettings(stock_lengths_mm=(10000, 12000), time_limit_s=0.01)
    result = planner.plan(hard_size, settings_)  # verify_plan runs inside
    size_plan = result.sizes[0]
    # Enumeration gives up at once and column generation takes over; it may even
    # prove optimality, but only when the plan meets its lower bound.
    if size_plan.status is SolveStatus.OPTIMAL:
        assert size_plan.lower_bound_mm == size_plan.total_stock_mm
    assert size_plan.total_stock_mm <= greedy_total(hard_size, (10000, 12000), 0)


def test_same_seed_same_totals_when_optimal() -> None:
    """With 8 CP-SAT workers the *choice* among equally short optimal plans can
    vary between runs; the optimal total (what is bought) cannot."""
    pieces = make_pieces(PRESETS["small"], seed=0)
    settings_ = OptimizerSettings(stock_lengths_mm=(10000, 12000), kerf_mm=3, random_seed=7)

    def fingerprint() -> list[tuple[str, int, SolveStatus, int | None]]:
        result = planner.plan(pieces, settings_)
        return [(s.size, s.total_stock_mm, s.status, s.lower_bound_mm) for s in result.sizes]

    first = fingerprint()
    assert all(status is SolveStatus.OPTIMAL for _, _, status, _ in first)
    assert fingerprint() == first
    assert fingerprint() == first


def test_greedy_only_plans_are_fully_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make the ILP find nothing so that every size keeps its greedy result.
    monkeypatch.setattr(planner, "solve_patterns", lambda *_, **__: None)
    pieces = make_pieces(PRESETS["medium"], seed=0)
    settings_ = OptimizerSettings(stock_lengths_mm=(10000, 12000))
    runs = [planner.plan(pieces, settings_) for _ in range(3)]
    assert all(s.status is SolveStatus.GREEDY for s in runs[0].sizes)
    assert runs[0].sizes == runs[1].sizes == runs[2].sizes


def test_cancel_during_cp_sat_is_prompt(hard_size: list[Piece]) -> None:
    token = CancelToken()
    settings_ = OptimizerSettings(stock_lengths_mm=(10000, 12000), time_limit_s=60)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            planner.plan(hard_size, settings_, cancel=token)
        except BaseException as exc:  # collected for the assertion below
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(1.5)  # greedy + enumeration are done; CP-SAT is searching
    cancelled_at = time.monotonic()
    token.cancel()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert time.monotonic() - cancelled_at < 2.5
    assert len(outcome) == 1 and isinstance(outcome[0], Cancelled)
