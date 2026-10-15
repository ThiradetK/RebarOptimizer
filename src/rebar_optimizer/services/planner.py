"""Per-size orchestration (5.1): split oversize pieces, solve, post-process,
map lengths back to piece numbers, group identical bars and verify.

Inventory (phase 8) is passed per size. Plans are compared by ``stock_cost``:
purchased length first, then inventory length used. Without inventory that is
the plain total stock length of D2.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from rebar_optimizer.domain.errors import Issue, IssueCode
from rebar_optimizer.domain.models import (
    Bar,
    BarGroup,
    Cut,
    CuttingPlan,
    InventoryStock,
    Piece,
    Remnant,
    SizePlan,
    SolveStatus,
    StockSource,
    Unplaced,
)
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.services.cancel import (
    CancelToken,
    ProgressCallback,
    ProgressEvent,
    ProgressKind,
    Stage,
    StageCallback,
    check_cancelled,
)
from rebar_optimizer.services.invariants import verify_plan
from rebar_optimizer.services.postprocess import classify_remnant, postprocess
from rebar_optimizer.services.summary import bar_detail
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import (
    NO_INVENTORY,
    Demand,
    Inventory,
    RawBar,
    stock_cost,
    total_stock_mm,
)
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
    inventory: Sequence[InventoryStock] = (),
) -> CuttingPlan:
    """Plan every size in input order. Raises ``SettingsError``, ``Cancelled``
    or ``InvariantViolation``. Inventory of sizes without pieces is ignored."""
    settings.validate()
    inventory_by_size = inventory_per_size(inventory)
    sizes = list(dict.fromkeys(p.size for p in pieces))
    size_plans: list[SizePlan] = []
    issues: list[Issue] = []
    for index, size in enumerate(sizes):
        check_cancelled(cancel)
        if on_progress is not None:
            on_progress(ProgressEvent(ProgressKind.SIZE_STARTED, size, index, len(sizes)))
        size_pieces = [p for p in pieces if p.size == size]
        size_plans.append(
            plan_size(
                size,
                size_pieces,
                settings,
                cancel,
                issues,
                inventory=inventory_by_size.get(size, NO_INVENTORY),
                on_stage=_stage_reporter(on_progress, size, index, len(sizes)),
            )
        )
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
    result = CuttingPlan(
        sizes=tuple(size_plans), settings=settings, issues=tuple(merge_size_issues(issues))
    )
    verify_plan(pieces, result, settings, inventory)
    return result


def _stage_reporter(
    on_progress: ProgressCallback | None, size: str, index: int, total: int
) -> StageCallback | None:
    if on_progress is None:
        return None

    def report(stage: Stage, step: int, steps: int) -> None:
        on_progress(ProgressEvent(ProgressKind.STAGE, size, index, total, stage, step, steps))

    return report


def merge_size_issues(issues: Sequence[Issue]) -> list[Issue]:
    """One issue per code instead of one per size when only the size differs,
    e.g. ``Size DB12, DB16: ...`` (first occurrence keeps its place)."""
    merged: dict[tuple[object, ...], tuple[Issue, list[object]]] = {}
    result: list[Issue | tuple[object, ...]] = []
    for issue in issues:
        if "size" not in issue.params:
            result.append(issue)
            continue
        rest = tuple(sorted((k, repr(v)) for k, v in issue.params.items() if k != "size"))
        key = (issue.code, issue.severity, rest)
        if key not in merged:
            merged[key] = (issue, [])
            result.append(key)
        merged[key][1].append(issue.params["size"])
    out: list[Issue] = []
    for item in result:
        if isinstance(item, Issue):
            out.append(item)
            continue
        first, sizes = merged[item]
        params = dict(first.params)
        params["size"] = sizes if len(sizes) > 1 else sizes[0]
        out.append(Issue(first.code, first.severity, params))
    return out


def inventory_per_size(inventory: Sequence[InventoryStock]) -> dict[str, dict[int, int]]:
    """``{size: {length_mm: bars available}}``; repeated lengths add up."""
    result: dict[str, dict[int, int]] = {}
    for item in inventory:
        lengths = result.setdefault(item.size, {})
        lengths[item.length_mm] = lengths.get(item.length_mm, 0) + item.quantity
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
    *,
    inventory: Inventory = NO_INVENTORY,
    on_stage: StageCallback | None = None,
) -> SizePlan:
    """Plan one size. Warnings (pattern limit, time limit) go to ``issues``;
    ``on_stage`` hears about each step so long sizes still show progress.

    Inventory is usable in both modes; single-length mode restricts only the
    purchased stock (phase 8). Pieces longer than the longest *new* stock are
    unplaced even if an inventory bar is longer: inventory is limited, so only
    new stock guarantees that every remaining piece can be placed.
    """
    issues = [] if issues is None else issues
    inventory = {length: n for length, n in inventory.items() if n > 0}
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
        # D4: only new lengths that hold the longest remaining piece; never drop pieces.
        candidate_sets = [(s,) for s in stocks if s >= max(demand)]
    else:
        candidate_sets = [stocks]

    # Single-length mode shares the size's time limit equally between lengths (5.1.3).
    time_limit = settings.time_limit_s / len(candidate_sets)
    solved_sets = []
    for step, stock_set in enumerate(candidate_sets):
        check_cancelled(cancel)

        def report(stage: Stage, step: int = step) -> None:
            if on_stage is not None:
                on_stage(stage, step, len(candidate_sets))

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
                    inventory=inventory,
                    report=report,
                ),
                stock_set,
            )
        )
    best, best_set = min(solved_sets, key=lambda item: stock_cost(item[0].bars))
    best_new, best_inventory = stock_cost(best.bars)
    # The size's bound must hold for every candidate length, not just the chosen one.
    lower_bound = min(solved.lower_bound_mm for solved, _ in solved_sets)
    status = best.status
    if status is SolveStatus.OPTIMAL and lower_bound < best_new:
        status = SolveStatus.FEASIBLE
    logger.info(
        "size %s: %d bars, %d mm stock (%d mm from inventory), status %s",
        size,
        len(best.bars),
        total_stock_mm(best.bars),
        best_inventory,
        best.status,
    )

    if on_stage is not None:
        on_stage(Stage.FINISH, 0, 1)
    groups = assign_numbers(size, best.bars, placeable)
    buys_new = any(not bar.is_inventory for bar in best.bars)
    return SizePlan(
        size=size,
        kerf_mm=settings.kerf_mm,
        groups=groups,
        unplaced=unplaced,
        remnants=summarize_remnants(groups, settings.kerf_mm, piece_lengths),
        piece_lengths_mm=piece_lengths,
        status=status,
        lower_bound_mm=lower_bound,
        single_stock_mm=(
            best_set[0] if settings.stock_mode is StockMode.SINGLE and buys_new else None
        ),
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
    inventory: Inventory = NO_INVENTORY,
    report: Callable[[Stage], None] = lambda _: None,
) -> _Solved:
    """Greedy first, then the ILP; keep the lower ``stock_cost`` (D8).

    ``time_limit_s`` covers this whole call; CP-SAT gets what is left.
    ``lower_bound_mm`` bounds the purchased length.
    """
    started = time.monotonic()
    kerf_mm = settings.kerf_mm
    report(Stage.GREEDY)
    greedy = GreedySolver().solve(demand, stock_set, kerf_mm, cancel, inventory=inventory)
    report(Stage.IMPROVE)
    greedy_bars = postprocess(greedy.bars, demand, stock_set, kerf_mm, cancel, inventory=inventory)
    greedy_cost = stock_cost(greedy_bars)
    bound = simple_lower_bound(demand, stock_set, kerf_mm, inventory)
    if _meets_bound(greedy_cost, bound):
        logger.info("size %s: greedy meets the lower bound, ILP skipped", size)
        return _Solved(bars=greedy_bars, status=SolveStatus.GREEDY, lower_bound_mm=bound)

    report(Stage.SEARCH)
    pattern_set = enumerate_patterns(
        demand,
        stock_set,
        kerf_mm,
        settings.max_patterns,
        extra_bars=greedy_bars,
        cancel=cancel,
        inventory=inventory,
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
            demand,
            stock_set,
            kerf_mm,
            options,
            initial_bars=greedy_bars,
            cancel=cancel,
            inventory=inventory,
        )
    else:
        result = solve_patterns(
            demand, pattern_set, options, hint=greedy_bars, cancel=cancel, inventory=inventory
        )
    if result is None:
        issues.append(Issue.warning(IssueCode.TIME_LIMIT_HIT, size=size, seconds=time_limit_s))
        return _Solved(bars=greedy_bars, status=SolveStatus.GREEDY, lower_bound_mm=bound)

    if result.lower_bound_mm is not None:
        bound = max(bound, result.lower_bound_mm)
    ilp_bars = postprocess(result.bars, demand, stock_set, kerf_mm, cancel, inventory=inventory)
    ilp_cost = stock_cost(ilp_bars)
    status = result.status
    if _meets_bound(ilp_cost, bound):
        # Post-processing (removing over-cuts, downsizing) reached the proven bound.
        status = SolveStatus.OPTIMAL
    if status is SolveStatus.FEASIBLE:
        issues.append(Issue.warning(IssueCode.TIME_LIMIT_HIT, size=size, seconds=time_limit_s))
    if ilp_cost <= greedy_cost:
        return _Solved(bars=ilp_bars, status=status, lower_bound_mm=bound)
    return _Solved(bars=greedy_bars, status=SolveStatus.GREEDY, lower_bound_mm=bound)


def _meets_bound(cost: tuple[int, int], bound: int) -> bool:
    """Provably optimal: purchase at the bound and no inventory left to economize on."""
    new, inventory = cost
    return new <= bound and inventory == 0


def simple_lower_bound(
    demand: Demand,
    stock_set: Sequence[int],
    kerf_mm: int,
    inventory: Mapping[int, int] = NO_INVENTORY,
) -> int:
    """A cheap valid bound on the purchased length, used when the ILP is
    skipped or gives a weaker one.

    Stock is at least the total piece length, and inventory supplies at most
    its total length. With a single new length ``S``, the knapsack weight
    ``sum(l + k)`` left after inventory (capacity ``S + k`` per bar) needs at
    least ``ceil(left / (S + k))`` new bars.
    """
    pieces_total = sum(length * count for length, count in demand.items())
    inventory_total = sum(length * count for length, count in inventory.items())
    bound = max(0, pieces_total - inventory_total)
    if len(stock_set) != 1:
        return bound
    stock = stock_set[0]
    weight = sum(kerf.item_size(length, kerf_mm) * count for length, count in demand.items())
    inventory_capacity = sum(
        kerf.capacity(length, kerf_mm) * count for length, count in inventory.items()
    )
    bars = math.ceil(max(0, weight - inventory_capacity) / kerf.capacity(stock, kerf_mm))
    return max(bound, bars * stock)


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

    ordered = sorted(
        bars,
        key=lambda b: (b.source is StockSource.NEW, b.stock_length_mm, b.lengths),
        reverse=True,
    )
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
        bar = Bar(
            size=size, stock_length_mm=raw.stock_length_mm, cuts=tuple(cuts), source=raw.source
        )
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
