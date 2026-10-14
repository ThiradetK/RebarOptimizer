"""Per-size orchestration (5.1): split oversize pieces, solve, post-process,
map lengths back to piece numbers, group identical bars and verify."""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass

from rebar_optimizer.domain.errors import Issue, IssueCode
from rebar_optimizer.domain.models import (
    Bar,
    BarGroup,
    Cut,
    CuttingPlan,
    Piece,
    Remnant,
    SizePlan,
    SolveStatus,
    Unplaced,
)
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.services.cancel import (
    CancelToken,
    ProgressCallback,
    ProgressEvent,
    ProgressKind,
    check_cancelled,
)
from rebar_optimizer.services.invariants import verify_plan
from rebar_optimizer.services.postprocess import classify_remnant, postprocess
from rebar_optimizer.services.summary import bar_detail
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import Demand, RawBar, total_stock_mm
from rebar_optimizer.solver.colgen import solve_colgen
from rebar_optimizer.solver.greedy import GreedySolver
from rebar_optimizer.solver.ilp import IlpOptions, solve_patterns
from rebar_optimizer.solver.patterns import enumerate_patterns

logger = logging.getLogger(__name__)

_DIGITS = re.compile(r"(\d+)")
MIN_ILP_TIME_S = 0.5
"""CP-SAT always gets at least this long, even if greedy used up the budget."""


def natural_key(text: str) -> tuple[tuple[int, int | str], ...]:
    """``"No2" < "No10"``; numbers sort before text at each position."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _DIGITS.split(text)
        if part
    )


def plan(
    pieces: Sequence[Piece],
    settings: OptimizerSettings,
    on_progress: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
) -> CuttingPlan:
    """Plan every size in input order. Raises ``SettingsError``, ``Cancelled``
    or ``InvariantViolation``."""
    settings.validate()
    sizes = list(dict.fromkeys(p.size for p in pieces))
    size_plans: list[SizePlan] = []
    issues: list[Issue] = []
    for index, size in enumerate(sizes):
        check_cancelled(cancel)
        if on_progress is not None:
            on_progress(ProgressEvent(ProgressKind.SIZE_STARTED, size, index, len(sizes)))
        size_pieces = [p for p in pieces if p.size == size]
        size_plans.append(plan_size(size, size_pieces, settings, cancel, issues))
        if on_progress is not None:
            on_progress(ProgressEvent(ProgressKind.SIZE_FINISHED, size, index, len(sizes)))

    unplaced = [u for sp in size_plans for u in sp.unplaced]
    if unplaced:
        issues.append(
            Issue.warning(
                IssueCode.PIECES_UNPLACED,
                count=sum(u.quantity for u in unplaced),
                nos=[u.no for u in unplaced],
            )
        )
    result = CuttingPlan(sizes=tuple(size_plans), settings=settings, issues=tuple(issues))
    verify_plan(pieces, result, settings)
    return result


@dataclass(frozen=True)
class _Solved:
    bars: list[RawBar]
    status: SolveStatus
    lower_bound_mm: int


def plan_size(
    size: str,
    pieces: Sequence[Piece],
    settings: OptimizerSettings,
    cancel: CancelToken | None = None,
    issues: list[Issue] | None = None,
) -> SizePlan:
    """Plan one size. Warnings (pattern limit, time limit) go to ``issues``."""
    issues = [] if issues is None else issues
    stocks = tuple(sorted(settings.stock_lengths_mm))
    longest_stock = stocks[-1]
    unplaced = tuple(
        Unplaced(p.size, p.no, p.length_mm, p.quantity, longest_stock)
        for p in pieces
        if p.length_mm > longest_stock
    )
    placeable = [p for p in pieces if p.length_mm <= longest_stock]
    demand: Counter[int] = Counter()
    for piece in placeable:
        demand[piece.length_mm] += piece.quantity
    piece_lengths = tuple(sorted({p.length_mm for p in pieces}))

    if not demand:
        return SizePlan(
            size=size,
            kerf_mm=settings.kerf_mm,
            groups=(),
            unplaced=unplaced,
            remnants=(),
            piece_lengths_mm=piece_lengths,
            status=SolveStatus.GREEDY,
            lower_bound_mm=0,
            single_stock_mm=None,
        )

    candidate_sets: list[tuple[int, ...]]
    if settings.stock_mode is StockMode.SINGLE:
        # D4: only lengths that hold the longest remaining piece; never drop pieces.
        candidate_sets = [(s,) for s in stocks if s >= max(demand)]
    else:
        candidate_sets = [stocks]

    # Single-length mode shares the size's time limit equally between lengths (5.1.3).
    time_limit = settings.time_limit_s / len(candidate_sets)
    solved_sets = []
    for stock_set in candidate_sets:
        check_cancelled(cancel)
        solved_sets.append(
            (
                _solve_lengths(
                    size,
                    demand,
                    stock_set,
                    settings=settings,
                    time_limit_s=time_limit,
                    cancel=cancel,
                    issues=issues,
                ),
                stock_set,
            )
        )
    best, best_set = min(solved_sets, key=lambda item: total_stock_mm(item[0].bars))
    best_total = total_stock_mm(best.bars)
    # The size's bound must hold for every candidate length, not just the chosen one.
    lower_bound = min(solved.lower_bound_mm for solved, _ in solved_sets)
    status = best.status
    if status is SolveStatus.OPTIMAL and lower_bound < best_total:
        status = SolveStatus.FEASIBLE
    logger.info(
        "size %s: %d bars, %d mm stock, status %s",
        size,
        len(best.bars),
        total_stock_mm(best.bars),
        best.status,
    )

    groups = assign_numbers(size, best.bars, placeable)
    return SizePlan(
        size=size,
        kerf_mm=settings.kerf_mm,
        groups=groups,
        unplaced=unplaced,
        remnants=summarize_remnants(groups, settings.kerf_mm, piece_lengths),
        piece_lengths_mm=piece_lengths,
        status=status,
        lower_bound_mm=lower_bound,
        single_stock_mm=best_set[0] if settings.stock_mode is StockMode.SINGLE else None,
    )


def _solve_lengths(
    size: str,
    demand: Demand,
    stock_set: tuple[int, ...],
    *,
    settings: OptimizerSettings,
    time_limit_s: float,
    cancel: CancelToken | None,
    issues: list[Issue],
) -> _Solved:
    """Greedy first, then the ILP; keep the shorter total (D8).

    ``time_limit_s`` covers this whole call; CP-SAT gets what is left.
    """
    started = time.monotonic()
    kerf_mm = settings.kerf_mm
    greedy = GreedySolver().solve(demand, stock_set, kerf_mm, cancel)
    greedy_bars = postprocess(greedy.bars, demand, stock_set, kerf_mm, cancel)
    greedy_total = total_stock_mm(greedy_bars)
    bound = simple_lower_bound(demand, stock_set, kerf_mm)
    if greedy_total <= bound:
        logger.info("size %s: greedy meets the lower bound, ILP skipped", size)
        return _Solved(bars=greedy_bars, status=SolveStatus.GREEDY, lower_bound_mm=bound)

    pattern_set = enumerate_patterns(
        demand, stock_set, kerf_mm, settings.max_patterns, extra_bars=greedy_bars, cancel=cancel
    )
    options = IlpOptions(
        time_limit_s=max(MIN_ILP_TIME_S, time_limit_s - (time.monotonic() - started)),
        random_seed=settings.random_seed,
        num_workers=settings.num_workers,
    )
    if pattern_set is None:
        # Too many patterns to enumerate: column generation (5.5).
        issues.append(
            Issue.warning(
                IssueCode.PATTERN_LIMIT_EXCEEDED, size=size, max_patterns=settings.max_patterns
            )
        )
        result = solve_colgen(
            demand, stock_set, kerf_mm, options, initial_bars=greedy_bars, cancel=cancel
        )
    else:
        result = solve_patterns(demand, pattern_set, options, hint=greedy_bars, cancel=cancel)
    if result is None:
        issues.append(Issue.warning(IssueCode.TIME_LIMIT_HIT, size=size, seconds=time_limit_s))
        return _Solved(bars=greedy_bars, status=SolveStatus.GREEDY, lower_bound_mm=bound)

    if result.lower_bound_mm is not None:
        bound = max(bound, result.lower_bound_mm)
    ilp_bars = postprocess(result.bars, demand, stock_set, kerf_mm, cancel)
    ilp_total = total_stock_mm(ilp_bars)
    status = result.status
    if ilp_total <= bound:
        # Post-processing (removing over-cuts, downsizing) reached the proven bound.
        status = SolveStatus.OPTIMAL
    if status is SolveStatus.FEASIBLE:
        issues.append(Issue.warning(IssueCode.TIME_LIMIT_HIT, size=size, seconds=time_limit_s))
    if ilp_total <= greedy_total:
        return _Solved(bars=ilp_bars, status=status, lower_bound_mm=bound)
    return _Solved(bars=greedy_bars, status=SolveStatus.GREEDY, lower_bound_mm=bound)


def simple_lower_bound(demand: Demand, stock_set: Sequence[int], kerf_mm: int) -> int:
    """A cheap valid bound, used when the ILP is skipped or gives a weaker one.

    Total stock is at least the total piece length. With a single stock
    length ``S`` at least ``ceil(sum(l + k) / (S + k))`` bars are needed.
    """
    pieces_total = sum(length * count for length, count in demand.items())
    if len(stock_set) != 1:
        return pieces_total
    stock = stock_set[0]
    weight = sum(kerf.item_size(length, kerf_mm) * count for length, count in demand.items())
    bars = math.ceil(weight / kerf.capacity(stock, kerf_mm))
    return max(pieces_total, bars * stock)


def assign_numbers(
    size: str, bars: Sequence[RawBar], pieces: Sequence[Piece]
) -> tuple[BarGroup, ...]:
    """Map lengths back to piece numbers (5.1 steps 7-8).

    Numbers are handed out in natural order of No, and identical bars are
    visited consecutively, so they draw the same numbers as far as possible.
    """
    queues: dict[int, deque[tuple[str, int]]] = {}
    for piece in sorted(pieces, key=lambda p: natural_key(p.no)):
        queues.setdefault(piece.length_mm, deque()).append((piece.no, piece.quantity))

    ordered = sorted(bars, key=lambda b: (b.stock_length_mm, b.lengths), reverse=True)
    counts: dict[Bar, int] = {}
    for raw in ordered:
        cuts: list[Cut] = []
        for length in raw.lengths:
            queue = queues[length]
            no, remaining = queue[0]
            cuts.append(Cut(no=no, length_mm=length))
            if remaining > 1:
                queue[0] = (no, remaining - 1)
            else:
                queue.popleft()
        cuts.sort(key=lambda c: (-c.length_mm, natural_key(c.no)))
        bar = Bar(size=size, stock_length_mm=raw.stock_length_mm, cuts=tuple(cuts))
        counts[bar] = counts.get(bar, 0) + 1
    leftover = {length: list(q) for length, q in queues.items() if q}
    if leftover:  # pragma: no cover - verify_plan would also catch this
        raise AssertionError(f"pieces left unassigned: {leftover}")
    return tuple(BarGroup(bar=bar, count=count) for bar, count in counts.items())


def summarize_remnants(
    groups: Sequence[BarGroup], kerf_mm: int, piece_lengths: tuple[int, ...]
) -> tuple[Remnant, ...]:
    """Reusable remnants grouped by length, longest first (D5 step 4)."""
    counts: Counter[int] = Counter()
    for group in groups:
        detail = bar_detail(group, kerf_mm, piece_lengths)
        if detail.is_reusable:
            counts[detail.usage.remnant_mm] += group.count
    return tuple(
        Remnant(
            length_mm=length,
            count=count,
            longest_cuttable_mm=classify_remnant(length, piece_lengths) or 0,
        )
        for length, count in sorted(counts.items(), reverse=True)
    )
