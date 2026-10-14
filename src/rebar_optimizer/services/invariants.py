"""The main invariant (D7): no piece may vanish silently.

Everything is recomputed from the raw plan and the input pieces; figures the
solver or planner reported are only ever *compared*, never trusted.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from rebar_optimizer.domain.errors import InvariantViolation
from rebar_optimizer.domain.models import CuttingPlan, Piece, PieceKey, SizePlan
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.solver import kerf


def verify_plan(pieces: Sequence[Piece], plan: CuttingPlan, settings: OptimizerSettings) -> None:
    """Raise ``InvariantViolation`` on the first broken rule."""
    expected = {p.key: p for p in pieces}
    _check_sizes(pieces, plan)
    _check_counts(expected, plan)
    allowed = set(settings.stock_lengths_mm)
    longest = max(allowed)
    for size_plan in plan.sizes:
        _check_bars(size_plan, settings, allowed)
        _check_unplaced(size_plan, expected, longest)
        _check_totals(size_plan, expected)


def _fail(message: str) -> InvariantViolation:
    return InvariantViolation(message)


def _check_sizes(pieces: Sequence[Piece], plan: CuttingPlan) -> None:
    planned = [sp.size for sp in plan.sizes]
    if len(planned) != len(set(planned)):
        raise _fail(f"size planned twice: {planned}")
    if set(planned) != {p.size for p in pieces}:
        raise _fail(f"planned sizes {sorted(planned)} differ from input sizes")


def _check_counts(expected: dict[PieceKey, Piece], plan: CuttingPlan) -> None:
    """Rule 1: cut + unplaced == input quantity for every (size, no)."""
    cut: Counter[PieceKey] = Counter()
    unplaced: Counter[PieceKey] = Counter()
    for size_plan in plan.sizes:
        for group in size_plan.groups:
            if group.count <= 0:
                raise _fail(f"non-positive group count {group.count} in {size_plan.size}")
            if group.bar.size != size_plan.size:
                raise _fail(f"bar of size {group.bar.size} filed under {size_plan.size}")
            for c in group.bar.cuts:
                key = (size_plan.size, c.no)
                piece = expected.get(key)
                if piece is None:
                    raise _fail(f"plan cuts unknown piece {key}")
                if c.length_mm != piece.length_mm:
                    raise _fail(
                        f"piece {key} cut at {c.length_mm} mm, input says {piece.length_mm}"
                    )
                cut[key] += group.count
        for u in size_plan.unplaced:
            unplaced[(u.size, u.no)] += u.quantity
    for key, piece in expected.items():
        total = cut[key] + unplaced[key]
        if total != piece.quantity:
            raise _fail(
                f"piece {key}: cut {cut[key]} + unplaced {unplaced[key]} != input {piece.quantity}"
            )
    extra = (set(cut) | set(unplaced)) - set(expected)
    if extra:
        raise _fail(f"plan contains pieces not in input: {sorted(extra)}")


def _check_bars(size_plan: SizePlan, settings: OptimizerSettings, allowed: set[int]) -> None:
    """Rules 2 and 3: every bar fits, kerf accounting adds up, stock allowed."""
    if size_plan.kerf_mm != settings.kerf_mm:
        raise _fail(f"{size_plan.size}: kerf {size_plan.kerf_mm} != settings {settings.kerf_mm}")
    used_stocks: set[int] = set()
    for group in size_plan.groups:
        bar = group.bar
        lengths = [c.length_mm for c in bar.cuts]
        if not lengths:
            raise _fail(f"{size_plan.size}: empty bar")
        if bar.stock_length_mm not in allowed:
            raise _fail(f"{size_plan.size}: stock {bar.stock_length_mm} not in {sorted(allowed)}")
        if not kerf.fits(lengths, bar.stock_length_mm, settings.kerf_mm):
            raise _fail(f"{size_plan.size}: {lengths} exceed {bar.stock_length_mm} mm")
        usage = kerf.bar_usage(lengths, bar.stock_length_mm, settings.kerf_mm)
        if usage.pieces_mm + usage.kerf_loss_mm + usage.remnant_mm != bar.stock_length_mm:
            raise _fail(f"{size_plan.size}: kerf accounting broken for {bar}")
        used_stocks.add(bar.stock_length_mm)
    if settings.stock_mode is StockMode.SINGLE and size_plan.groups:
        if len(used_stocks) != 1:
            raise _fail(f"{size_plan.size}: single-length mode uses {sorted(used_stocks)}")
        if size_plan.single_stock_mm not in used_stocks:
            raise _fail(f"{size_plan.size}: chosen length {size_plan.single_stock_mm} unused")


def _check_unplaced(size_plan: SizePlan, expected: dict[PieceKey, Piece], longest: int) -> None:
    """Rule 4: unplaced pieces really are longer than the longest stock."""
    for u in size_plan.unplaced:
        piece = expected.get((u.size, u.no))
        if piece is None or piece.length_mm != u.length_mm:
            raise _fail(f"unplaced {u} does not match the input")
        if u.length_mm <= longest:
            raise _fail(f"unplaced {u.size}/{u.no} ({u.length_mm} mm) fits stock {longest} mm")
        if u.size != size_plan.size:
            raise _fail(f"unplaced {u.size}/{u.no} filed under {size_plan.size}")


def _check_totals(size_plan: SizePlan, expected: dict[PieceKey, Piece]) -> None:
    """Rule 5: reported figures equal what the bars add up to."""
    lengths = tuple(sorted({p.length_mm for p in expected.values() if p.size == size_plan.size}))
    if size_plan.piece_lengths_mm != lengths:
        raise _fail(f"{size_plan.size}: piece lengths {size_plan.piece_lengths_mm} != {lengths}")

    reusable: Counter[int] = Counter()
    for group in size_plan.groups:
        usage = kerf.bar_usage(
            [c.length_mm for c in group.bar.cuts], group.bar.stock_length_mm, size_plan.kerf_mm
        )
        if usage.remnant_mm and any(length <= usage.remnant_mm for length in lengths):
            reusable[usage.remnant_mm] += group.count
    reported = Counter({r.length_mm: r.count for r in size_plan.remnants})
    if len(size_plan.remnants) != len(reported) or reported != reusable:
        raise _fail(f"{size_plan.size}: reusable remnants {dict(reported)} != {dict(reusable)}")
    for remnant in size_plan.remnants:
        longest = max(length for length in lengths if length <= remnant.length_mm)
        if remnant.longest_cuttable_mm != longest:
            raise _fail(f"{size_plan.size}: remnant {remnant} longest cuttable should be {longest}")

    bound = size_plan.lower_bound_mm
    if bound is not None and bound > size_plan.total_stock_mm:
        raise _fail(
            f"{size_plan.size}: lower bound {bound} above result {size_plan.total_stock_mm}"
        )
