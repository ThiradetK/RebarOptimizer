"""Column generation for sizes with too many patterns to enumerate (5.5).

1. Master LP over a growing pattern set (GLOP), starting from the greedy
   patterns plus one single-piece pattern per length.
2. Pricing per stock length ``S``: bounded knapsack maximizing ``sum(pi_i a_i)``
   subject to ``sum((l_i + k) a_i) <= S + k`` and ``a_i <= d_i``. A pattern
   enters when ``sum(pi_i a_i) > S`` (negative reduced cost).
3. When no pattern enters, the LP value is a lower bound. If the loop stops
   early, Farley's bound ``z_LP * min_S(S / best_S)`` is used instead, which
   is valid at every iteration.
4. CP-SAT solves the integer problem over the generated patterns -> HEURISTIC.

With inventory (phase 8) the LP treats every inventory length as unlimited
stock. That relaxes the problem, so the LP bound on *total* stock stays valid,
and ``bound - total inventory length`` bounds the purchased length. Each column
then becomes a pattern on new stock and/or on inventory of its length, and the
integer step enforces availability (``solve_patterns``).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

import numpy as np
from ortools.linear_solver import pywraplp

from rebar_optimizer.domain.models import SolveStatus, StockSource
from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import NO_INVENTORY, Demand, Inventory, RawBar, SolveResult
from rebar_optimizer.solver.greedy import GreedySolver
from rebar_optimizer.solver.ilp import IlpOptions, solve_patterns
from rebar_optimizer.solver.patterns import Pattern, PatternSet, pattern_of

logger = logging.getLogger(__name__)

REDUCED_COST_EPS = 1e-6
MAX_ITERATIONS = 2000
LP_SHARE_OF_TIME = 0.5
"""Column generation may use at most this share of the size's time budget."""


@dataclass(frozen=True)
class PricingResult:
    value: float
    counts: tuple[int, ...]


def price(
    duals: Sequence[float], weights: Sequence[int], caps: Sequence[int], capacity: int
) -> PricingResult:
    """Bounded knapsack: maximize ``sum(duals_i * a_i)`` with ``sum(weights_i * a_i) <= capacity``
    and ``0 <= a_i <= caps_i`` (dynamic programming over capacity, binary-split items)."""
    n = len(weights)
    divisor = math.gcd(capacity, *weights)
    size = capacity // divisor
    chunks: list[tuple[int, float, int, int]] = []  # (weight, value, item, multiplicity)
    for i in range(n):
        if duals[i] <= 0 or weights[i] > capacity:
            continue
        remaining = min(caps[i], capacity // weights[i])
        multiplicity = 1
        while remaining > 0:
            m = min(multiplicity, remaining)
            chunks.append((weights[i] // divisor * m, duals[i] * m, i, m))
            remaining -= m
            multiplicity *= 2

    best = np.zeros(size + 1)
    taken: list[np.ndarray] = []
    for weight, value, _, _ in chunks:
        candidate = best[: size + 1 - weight] + value
        take = np.zeros(size + 1, dtype=bool)
        take[weight:] = candidate > best[weight:] + 1e-12
        best = best.copy()
        best[weight:] = np.where(take[weight:], candidate, best[weight:])
        taken.append(take)

    counts = [0] * n
    j = size
    for (weight, _, item, m), take in zip(reversed(chunks), reversed(taken), strict=True):
        if take[j]:
            counts[item] += m
            j -= weight
    value = sum(duals[i] * counts[i] for i in range(n))
    return PricingResult(value=value, counts=tuple(counts))


class _MasterLp:
    """Restricted master LP: min sum(S_p x_p), sum(a_ip x_p) >= d_i, x >= 0."""

    def __init__(self, lengths: Sequence[int], demand: Demand) -> None:
        self.lp = pywraplp.Solver.CreateSolver("GLOP")
        self.rows = [self.lp.Constraint(float(demand[n]), self.lp.infinity()) for n in lengths]
        self.objective = self.lp.Objective()
        self.objective.SetMinimization()
        self.columns: dict[Pattern, None] = {}

    def add(self, pattern: Pattern) -> bool:
        # The LP only sees lengths; the source is decided in ``_integer_patterns``.
        pattern = replace(pattern, source=StockSource.NEW)
        if pattern in self.columns:
            return False
        self.columns[pattern] = None
        var = self.lp.NumVar(0.0, self.lp.infinity(), "")
        self.objective.SetCoefficient(var, float(pattern.stock_length_mm))
        for row, count in zip(self.rows, pattern.counts, strict=True):
            if count:
                row.SetCoefficient(var, float(count))
        return True

    def solve(self) -> tuple[float, list[float]] | None:
        """``(objective, duals)`` or ``None`` if GLOP did not reach optimality."""
        if self.lp.Solve() != pywraplp.Solver.OPTIMAL:
            return None
        return self.objective.Value(), [row.dual_value() for row in self.rows]


def _initial_columns(
    lengths: Sequence[int], stocks: Sequence[int], kerf_mm: int, bars: Sequence[RawBar]
) -> list[Pattern]:
    """Greedy patterns plus one single-piece pattern per length (always feasible)."""
    columns = [pattern_of(bar, lengths) for bar in bars]
    for i, length in enumerate(lengths):
        stock = kerf.shortest_fitting_stock([length], stocks, kerf_mm)
        if stock is not None:
            counts = [0] * len(lengths)
            counts[i] = 1
            columns.append(Pattern(stock, tuple(counts)))
    return columns


def _generate_columns(
    master: _MasterLp,
    stocks: Sequence[int],
    *,
    weights: Sequence[int],
    caps: Sequence[int],
    kerf_mm: int,
    deadline: float,
    cancel: CancelToken | None,
) -> tuple[float, bool, int]:
    """Run the pricing loop; return ``(lower bound, converged, iterations)``."""
    lower_bound = 0.0
    for iteration in range(1, MAX_ITERATIONS + 1):
        check_cancelled(cancel)
        solved = master.solve()
        if solved is None:
            logger.warning("master LP not optimal; stopping column generation")
            return lower_bound, False, iteration
        z, duals = solved
        added = False
        ratio = 1.0
        for stock in stocks:
            priced = price(duals, weights, caps, kerf.capacity(stock, kerf_mm))
            if priced.value > 0:
                ratio = min(ratio, stock / priced.value)
            if priced.value > stock * (1 + REDUCED_COST_EPS):
                added = master.add(Pattern(stock, priced.counts)) or added
        # Farley: duals scaled by ``ratio`` are dual feasible, so z * ratio is a bound.
        lower_bound = max(lower_bound, z * ratio)
        if not added:
            return max(lower_bound, z), True, iteration
        if time.monotonic() > deadline:
            return lower_bound, False, iteration
    return lower_bound, False, MAX_ITERATIONS


def solve_colgen(
    demand: Demand,
    stock_lengths_mm: Sequence[int],
    kerf_mm: int,
    options: IlpOptions,
    *,
    initial_bars: Sequence[RawBar] = (),
    cancel: CancelToken | None = None,
    inventory: Inventory = NO_INVENTORY,
) -> SolveResult | None:
    """Column generation + CP-SAT over the generated columns.

    The result's status is ``HEURISTIC``; ``lower_bound_mm`` comes from the LP
    and bounds the purchased length of the whole problem. ``None`` if CP-SAT
    found nothing.
    """
    started = time.monotonic()
    lengths = tuple(sorted(demand, reverse=True))
    inventory = {s: n for s, n in inventory.items() if n > 0}
    if not initial_bars:
        # 5.5 step 1 starts from the greedy patterns; the planner passes them in.
        initial_bars = (
            GreedySolver()
            .solve(demand, tuple(stock_lengths_mm), kerf_mm, cancel, inventory=inventory)
            .bars
        )
    new_stocks = sorted(stock_lengths_mm)
    stocks = sorted(set(new_stocks) | set(inventory))
    master = _MasterLp(lengths, demand)
    for pattern in _initial_columns(lengths, stocks, kerf_mm, initial_bars):
        master.add(pattern)

    lower_bound, converged, iterations = _generate_columns(
        master,
        stocks,
        weights=[kerf.item_size(length, kerf_mm) for length in lengths],
        caps=[demand[length] for length in lengths],
        kerf_mm=kerf_mm,
        deadline=started + options.time_limit_s * LP_SHARE_OF_TIME,
        cancel=cancel,
    )
    divisor = math.gcd(*stocks)
    bound_mm = math.ceil(lower_bound / divisor - 1e-6) * divisor
    bound_mm = max(0, bound_mm - sum(s * n for s, n in inventory.items()))
    logger.info(
        "column generation: %d iterations, %d columns, LP bound %.1f mm (%s)",
        iterations,
        len(master.columns),
        lower_bound,
        "converged" if converged else "stopped early, Farley bound",
    )

    patterns = _integer_patterns(
        master.columns,
        lengths,
        new_stocks,
        inventory=inventory,
        kerf_mm=kerf_mm,
        initial_bars=initial_bars,
    )
    pattern_set = PatternSet(lengths=lengths, patterns=patterns)
    remaining = max(0.5, options.time_limit_s - (time.monotonic() - started))
    result = solve_patterns(
        demand,
        pattern_set,
        IlpOptions(remaining, options.random_seed, options.num_workers),
        hint=initial_bars,
        cancel=cancel,
        inventory=inventory,
    )
    if result is None:
        return None
    return SolveResult(bars=result.bars, status=SolveStatus.HEURISTIC, lower_bound_mm=bound_mm)


def _integer_patterns(
    columns: Iterable[Pattern],
    lengths: Sequence[int],
    new_stocks: Sequence[int],
    *,
    inventory: Inventory,
    kerf_mm: int,
    initial_bars: Sequence[RawBar],
) -> tuple[Pattern, ...]:
    """LP columns as patterns on each source that has their length, plus the
    initial bars and single pieces on new stock (always feasible)."""
    found: dict[Pattern, None] = {}
    for column in columns:
        if column.stock_length_mm in new_stocks:
            found[column] = None
        if column.stock_length_mm in inventory:
            found[replace(column, source=StockSource.INVENTORY)] = None
    for column in _initial_columns(lengths, new_stocks, kerf_mm, initial_bars):
        found.setdefault(column, None)
    return tuple(found)
