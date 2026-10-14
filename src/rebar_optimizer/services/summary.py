"""Derived per-bar and per-size figures, always recomputed from the raw plan."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rebar_optimizer.domain.models import BarGroup, SizePlan
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
