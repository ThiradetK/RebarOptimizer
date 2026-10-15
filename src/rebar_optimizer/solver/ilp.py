"""CP-SAT model over an enumerated pattern set (5.4).

minimize   sum_p  S_p * x_p              (divided by the gcd of the S_p)
subject to sum_p  a_ip * x_p >= d_i      for every length i
           0 <= x_p <= U_p = max_i ceil(d_i / a_ip)

With inventory (phase 8) patterns on inventory lengths are limited by
``sum_{p on inventory length s} x_p <= available_s`` and the objective is
lexicographic, solved in two phases: (1) minimize the purchased length
``sum_new S_p x_p``; (2) add ``sum_new S_p x_p <= `` the phase-1 value and
minimize the inventory length used. A single weighted objective
(``W * new + inventory``) was tried first; its huge coefficients made CP-SAT
~10x slower on the medium benchmark with remnant inventory.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

from ortools.sat.python import cp_model

from rebar_optimizer.domain.errors import Cancelled
from rebar_optimizer.domain.models import SolveStatus, StockSource
from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver.base import NO_INVENTORY, Demand, Inventory, RawBar, SolveResult
from rebar_optimizer.solver.patterns import Pattern, PatternSet, pattern_counts

logger = logging.getLogger(__name__)

BUILD_CANCEL_EVERY = 2048
"""Patterns added between cancel checks while building the model."""

CANCEL_POLL_S = 0.1
PURCHASE_SHARE_OF_TIME = 0.7
"""With inventory, phase 1 (purchase) may use this share of the time limit."""
MIN_PHASE_TIME_S = 0.5


@dataclass(frozen=True)
class IlpOptions:
    time_limit_s: float
    random_seed: int = 0
    num_workers: int = 8


@dataclass(frozen=True)
class _Phase:
    values: list[int]
    objective: int
    bound: int
    """Objective bound, in gcd units."""
    proven: bool


def solve_patterns(
    demand: Demand,
    pattern_set: PatternSet,
    options: IlpOptions,
    hint: Sequence[RawBar] = (),
    cancel: CancelToken | None = None,
    *,
    inventory: Inventory = NO_INVENTORY,
) -> SolveResult | None:
    """Solve the pattern ILP. ``None`` if no solution was found in time.

    Status is ``OPTIMAL`` when CP-SAT proves optimality (of both phases with
    inventory), else ``FEASIBLE``. ``lower_bound_mm`` bounds the purchased
    length. The returned bars may over-cut (``>=`` constraints); post-process
    them. Patterns on inventory lengths missing from ``inventory`` are dropped.
    Raises ``Cancelled`` if ``cancel`` fires during the search.
    """
    check_cancelled(cancel)
    started = time.monotonic()
    lengths = pattern_set.lengths
    inventory = {s: n for s, n in inventory.items() if n > 0}
    patterns = tuple(
        p
        for p in pattern_set.patterns
        if p.source is StockSource.NEW or p.stock_length_mm in inventory
    )
    divisor = math.gcd(*(p.stock_length_mm for p in patterns))

    model, variables = _build_model(demand, lengths, patterns, inventory, cancel)

    def cost(source: StockSource) -> cp_model.LinearExprT:
        chosen = [
            (x, p.stock_length_mm // divisor)
            for p, x in zip(patterns, variables, strict=True)
            if p.source is source
        ]
        return cp_model.LinearExpr.weighted_sum([x for x, _ in chosen], [c for _, c in chosen])

    hinted = pattern_counts(hint, lengths)
    if hinted:
        for pattern, x in zip(patterns, variables, strict=True):
            model.add_hint(x, hinted.get(pattern, 0))

    two_phase = any(p.source is StockSource.INVENTORY for p in patterns)
    new_cost = cost(StockSource.NEW)
    model.minimize(new_cost)
    share = PURCHASE_SHARE_OF_TIME if two_phase else 1.0
    # ``time_limit_s`` covers this whole call: building a large model takes seconds.
    building = time.monotonic() - started
    phase_limit = max(MIN_PHASE_TIME_S, options.time_limit_s * share - building)
    phase = _run(model, variables, options, phase_limit, cancel)
    if phase is None:
        return None
    _log("purchase", len(patterns), divisor, phase)
    values, proven, bound = phase.values, phase.proven, phase.bound * divisor

    if two_phase:
        model.add(new_cost <= phase.objective)
        model.clear_hints()  # type: ignore[no-untyped-call]
        for x, value in zip(variables, values, strict=True):
            model.add_hint(x, value)
        model.minimize(cost(StockSource.INVENTORY))
        remaining = options.time_limit_s - (time.monotonic() - started)
        second = _run(model, variables, options, max(MIN_PHASE_TIME_S, remaining), cancel)
        if second is None:
            proven = False  # the phase-1 plan stands
        else:
            _log("inventory", len(patterns), divisor, second)
            values, proven = second.values, proven and second.proven

    bars: list[RawBar] = []
    for pattern, count in zip(patterns, values, strict=True):
        bars.extend(pattern_set.to_raw_bar(pattern) for _ in range(count))
    return SolveResult(
        bars=tuple(bars),
        status=SolveStatus.OPTIMAL if proven else SolveStatus.FEASIBLE,
        lower_bound_mm=bound,
    )


def _build_model(
    demand: Demand,
    lengths: Sequence[int],
    patterns: Sequence[Pattern],
    inventory: Inventory,
    cancel: CancelToken | None = None,
) -> tuple[cp_model.CpModel, list[cp_model.IntVar]]:
    """Variables, demand rows and inventory limits; the objective is set per phase.

    Tens of thousands of patterns take seconds to add, so the cancel token is
    polled while building, and rows use ``LinearExpr.weighted_sum`` (built in
    C++) rather than Python ``sum`` over expressions.
    """
    model = cp_model.CpModel()
    variables: list[cp_model.IntVar] = []
    for index, pattern in enumerate(patterns):
        if index % BUILD_CANCEL_EVERY == 0:
            check_cancelled(cancel)
        upper = max(
            math.ceil(demand[length] / count)
            for length, count in zip(lengths, pattern.counts, strict=True)
            if count
        )
        if pattern.source is StockSource.INVENTORY:
            upper = min(upper, inventory[pattern.stock_length_mm])
        variables.append(model.new_int_var(0, upper, f"x{index}"))
    rows: list[tuple[list[cp_model.IntVar], list[int]]] = [([], []) for _ in lengths]
    for pattern, x in zip(patterns, variables, strict=True):
        for i, count in enumerate(pattern.counts):
            if count:
                rows[i][0].append(x)
                rows[i][1].append(count)
    check_cancelled(cancel)
    for (row_vars, row_counts), length in zip(rows, lengths, strict=True):
        model.add(cp_model.LinearExpr.weighted_sum(row_vars, row_counts) >= demand[length])
    for stock, available in inventory.items():
        on_stock = [
            x
            for p, x in zip(patterns, variables, strict=True)
            if p.source is StockSource.INVENTORY and p.stock_length_mm == stock
        ]
        if len(on_stock) > 1:
            model.add(sum(on_stock) <= available)
    return model, variables


def _run(
    model: cp_model.CpModel,
    variables: Sequence[cp_model.IntVar],
    options: IlpOptions,
    time_limit_s: float,
    cancel: CancelToken | None,
) -> _Phase | None:
    check_cancelled(cancel)  # building a large model takes a moment
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.random_seed = options.random_seed
    solver.parameters.num_workers = options.num_workers
    # The objective is integral, so a gap below 1 already proves optimality.
    solver.parameters.absolute_gap_limit = 0.99
    # Measured on the medium benchmark (OR-Tools 9.15): the quick_restart worker
    # kept running ~20 s past the end of the search, and the big-linear-overlap
    # presolve step cost several seconds on 20k+ pattern models. Without them the
    # same instances solve 2-10x faster and stop promptly (cancel stays responsive).
    solver.parameters.ignore_subsolvers.append("quick_restart")
    solver.parameters.find_big_linear_overlap = False
    status = _solve_cancellable(solver, model, cancel)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        logger.info("CP-SAT found no solution (status %s)", status.name)
        return None
    objective = round(solver.objective_value)
    bound = min(objective, math.ceil(solver.best_objective_bound - 1e-6))
    logger.debug("CP-SAT %s in %.2fs", status.name, solver.wall_time)
    return _Phase(
        values=[solver.value(x) for x in variables],
        objective=objective,
        bound=bound,
        proven=status == cp_model.OPTIMAL or bound >= objective,
    )


def _log(what: str, pattern_count: int, divisor: int, phase: _Phase) -> None:
    logger.info(
        "CP-SAT %s %s: %d patterns, objective %d mm, bound %d mm",
        what,
        "OPTIMAL" if phase.proven else "FEASIBLE",
        pattern_count,
        phase.objective * divisor,
        phase.bound * divisor,
    )


def _solve_cancellable(
    solver: cp_model.CpSolver, model: cp_model.CpModel, cancel: CancelToken | None
) -> cp_model.CpSolverStatus:
    """Run the search; a watcher thread calls ``stop_search`` when cancelled (D9)."""
    if cancel is None:
        return solver.solve(model)
    done = threading.Event()

    def watch() -> None:
        while not done.is_set():
            if cancel.wait(CANCEL_POLL_S):
                # Repeat: a stop requested just before the search starts can be lost.
                while not done.is_set():
                    solver.stop_search()
                    done.wait(CANCEL_POLL_S)
                return

    watcher = threading.Thread(target=watch, name="cp-sat-cancel", daemon=True)
    watcher.start()
    try:
        status = solver.solve(model)
    finally:
        done.set()
        watcher.join()
    if cancel.is_cancelled:
        raise Cancelled
    return status
