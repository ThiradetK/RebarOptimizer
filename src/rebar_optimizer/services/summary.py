"""Derived per-bar and per-size figures, always recomputed from the raw plan."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from rebar_optimizer.domain.models import BarGroup, Cut, CuttingPlan, SizePlan
from rebar_optimizer.domain.weights import unit_weight_kg_per_m
from rebar_optimizer.services.postprocess import classify_remnant
from rebar_optimizer.solver import kerf


@dataclass(frozen=True)
class BarDetail:
    usage: kerf.BarUsage
    longest_cuttable_mm: int | None
    """Set when the remnant is reusable (D5 step 4); ``None`` means waste."""

    @property
    def is_reusable(self) -> bool:
        return self.longest_cuttable_mm is not None


@dataclass(frozen=True)
class SizeSummary:
    size: str
    bars_by_stock: dict[int, int]
    """``{stock_length_mm: bar count}``, shortest first."""
    bar_count: int
    stock_mm: int
    pieces_mm: int
    reusable_mm: int
    waste_mm: int
    kerf_mm: int
    piece_count: int

    @property
    def utilization(self) -> float:
        return self.pieces_mm / self.stock_mm if self.stock_mm else 0.0


def bar_detail(group: BarGroup, kerf_mm: int, piece_lengths_mm: tuple[int, ...]) -> BarDetail:
    lengths = [cut.length_mm for cut in group.bar.cuts]
    usage = kerf.bar_usage(lengths, group.bar.stock_length_mm, kerf_mm)
    longest = classify_remnant(usage.remnant_mm, piece_lengths_mm) if usage.remnant_mm else None
    return BarDetail(usage=usage, longest_cuttable_mm=longest)


def summarize_size(plan: SizePlan) -> SizeSummary:
    by_stock: Counter[int] = Counter()
    stock = pieces = reusable = waste = kerf_total = piece_count = 0
    for group in plan.groups:
        detail = bar_detail(group, plan.kerf_mm, plan.piece_lengths_mm)
        n = group.count
        by_stock[group.bar.stock_length_mm] += n
        stock += detail.usage.stock_length_mm * n
        pieces += detail.usage.pieces_mm * n
        kerf_total += detail.usage.kerf_loss_mm * n
        piece_count += detail.usage.piece_count * n
        if detail.is_reusable:
            reusable += detail.usage.remnant_mm * n
        else:
            waste += detail.usage.remnant_mm * n
    return SizeSummary(
        size=plan.size,
        bars_by_stock=dict(sorted(by_stock.items())),
        bar_count=sum(by_stock.values()),
        stock_mm=stock,
        pieces_mm=pieces,
        reusable_mm=reusable,
        waste_mm=waste,
        kerf_mm=kerf_total,
        piece_count=piece_count,
    )


def cut_runs(cuts: Sequence[Cut]) -> list[tuple[str, int, int]]:
    """Collapse consecutive identical cuts into ``(no, length_mm, count)``."""
    runs: list[tuple[str, int, int]] = []
    for cut in cuts:
        if runs and runs[-1][0] == cut.no and runs[-1][1] == cut.length_mm:
            runs[-1] = (cut.no, cut.length_mm, runs[-1][2] + 1)
        else:
            runs.append((cut.no, cut.length_mm, 1))
    return runs


@dataclass(frozen=True)
class PlanTotals:
    """Whole-plan figures for the GUI summary bar."""

    bar_count: int
    stock_mm: int
    pieces_mm: int
    reusable_mm: int
    waste_mm: int
    kerf_mm: int
    weight_kg: float | None
    """Purchase weight of the sizes whose weight is known; ``None`` if none is."""
    sizes_without_weight: tuple[str, ...]

    @property
    def utilization(self) -> float:
        return self.pieces_mm / self.stock_mm if self.stock_mm else 0.0


def plan_totals(plan: CuttingPlan) -> PlanTotals:
    summaries = [summarize_size(sp) for sp in plan.sizes]
    weights = [(s, unit_weight_kg_per_m(s.size)) for s in summaries if s.bar_count]
    known = [s.stock_mm / 1000 * w for s, w in weights if w is not None]
    return PlanTotals(
        bar_count=sum(s.bar_count for s in summaries),
        stock_mm=sum(s.stock_mm for s in summaries),
        pieces_mm=sum(s.pieces_mm for s in summaries),
        reusable_mm=sum(s.reusable_mm for s in summaries),
        waste_mm=sum(s.waste_mm for s in summaries),
        kerf_mm=sum(s.kerf_mm for s in summaries),
        weight_kg=sum(known) if known else None,
        sizes_without_weight=tuple(s.size for s, w in weights if w is None),
    )
