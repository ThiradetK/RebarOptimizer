"""CP-SAT model over an enumerated pattern set (5.4).

minimize   sum_p  S_p * x_p              (divided by the gcd of the S_p)
subject to sum_p  a_ip * x_p >= d_i      for every length i
           0 <= x_p <= U_p = max_i ceil(d_i / a_ip)
"""

from __future__ import annotations

import logging
import math
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ortools.sat.python import cp_model

from rebar_optimizer.domain.errors import Cancelled
from rebar_optimizer.domain.models import SolveStatus
from rebar_optimizer.services.cancel import CancelToken, check_cancelled
from rebar_optimizer.solver.base import Demand, RawBar, SolveResult
from rebar_optimizer.solver.patterns import PatternSet, pattern_of

logger = logging.getLogger(__name__)

CANCEL_POLL_S = 0.1


@dataclass(frozen=True)
class IlpOptions:
    time_limit_s: float
    random_seed: int = 0
    num_workers: int = 8


def solve_patterns(
    demand: Demand,
    pattern_set: PatternSet,
    options: IlpOptions,
    hint: Sequence[RawBar] = (),
    cancel: CancelToken | None = None,
) -> SolveResult | None:
    """Solve the pattern ILP. ``None`` if no solution was found in time.

    Status is ``OPTIMAL`` when CP-SAT proves optimality, else ``FEASIBLE``.
    The returned bars may over-cut (``>=`` constraints); post-process them.
    Raises ``Cancelled`` if ``cancel`` fires during the search.
    """
    check_cancelled(cancel)
    lengths = pattern_set.lengths
    patterns = pattern_set.patterns
    divisor = math.gcd(*(p.stock_length_mm for p in patterns))

    model = cp_model.CpModel()
    variables: list[cp_model.IntVar] = []
    for index, pattern in enumerate(patterns):
        upper = max(
            math.ceil(demand[length] / count)
            for length, count in zip(lengths, pattern.counts, strict=True)
            if count
        )
        variables.append(model.new_int_var(0, upper, f"x{index}"))
    for i, length in enumerate(lengths):
        model.add(
            sum(p.counts[i] * x for p, x in zip(patterns, variables, strict=True) if p.counts[i])
            >= demand[length]
        )
    model.minimize(
        sum((p.stock_length_mm // divisor) * x for p, x in zip(patterns, variables, strict=True))
    )

    hinted = Counter(pattern_of(bar, lengths) for bar in hint)
    if hinted:
        for pattern, x in zip(patterns, variables, strict=True):
            model.add_hint(x, hinted.get(pattern, 0))

    check_cancelled(cancel)  # building a large model takes a moment
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = options.time_limit_s
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
    bars: list[RawBar] = []
    for pattern, x in zip(patterns, variables, strict=True):
        bars.extend(pattern_set.to_raw_bar(pattern) for _ in range(solver.value(x)))
    objective = round(solver.objective_value)
    bound_units = min(objective, math.ceil(solver.best_objective_bound - 1e-6))
    proven = status == cp_model.OPTIMAL or bound_units >= objective
    bound = bound_units * divisor
    logger.info(
        "CP-SAT %s: %d patterns, objective %d mm, bound %d mm, %.2fs",
        status.name,
        len(patterns),
        objective * divisor,
        bound,
        solver.wall_time,
    )
    return SolveResult(
        bars=tuple(bars),
        status=SolveStatus.OPTIMAL if proven else SolveStatus.FEASIBLE,
        lower_bound_mm=bound,
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
