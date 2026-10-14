"""Mutation tests: deliberately broken plans must all be caught (guards against B8)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from rebar_optimizer.domain.errors import InvariantViolation
from rebar_optimizer.domain.models import (
    BarGroup,
    Cut,
    CuttingPlan,
    Piece,
    Remnant,
    Unplaced,
)
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.services import planner
from rebar_optimizer.services.invariants import verify_plan
from rebar_optimizer.solver import kerf

PIECES = (
    Piece("DB12", "1", 3500, 5, (2,)),
    Piece("DB12", "2", 2100, 7, (3,)),
    Piece("DB12", "3", 13000, 1, (4,)),
    Piece("DB16", "4", 4000, 3, (5,)),
)
SETTINGS = OptimizerSettings(stock_lengths_mm=(10000, 12000), kerf_mm=3)
SINGLE = replace(SETTINGS, stock_mode=StockMode.SINGLE)


@pytest.fixture(scope="module")
def good_plan() -> CuttingPlan:
    return planner.plan(PIECES, SETTINGS)


@pytest.fixture(scope="module")
def good_single_plan() -> CuttingPlan:
    return planner.plan(PIECES, SINGLE)


def with_size(plan: CuttingPlan, index: int, **changes: object) -> CuttingPlan:
    sizes = list(plan.sizes)
    sizes[index] = replace(sizes[index], **changes)  # type: ignore[arg-type]
    return replace(plan, sizes=tuple(sizes))


def with_group(
    plan: CuttingPlan, size_index: int, group_index: int, group: BarGroup
) -> CuttingPlan:
    groups = list(plan.sizes[size_index].groups)
    groups[group_index] = group
    return with_size(plan, size_index, groups=tuple(groups))


def first_group(plan: CuttingPlan) -> BarGroup:
    return plan.sizes[0].groups[0]


def test_good_plans_pass(good_plan: CuttingPlan, good_single_plan: CuttingPlan) -> None:
    verify_plan(PIECES, good_plan, SETTINGS)
    verify_plan(PIECES, good_single_plan, SINGLE)


Mutation = Callable[[CuttingPlan], CuttingPlan]


def drop_one_cut(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    return with_group(plan, 0, 0, replace(g, bar=replace(g.bar, cuts=g.bar.cuts[1:])))


def add_one_bar(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    return with_group(plan, 0, 0, replace(g, count=g.count + 1))


def zero_count_group(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    extra = BarGroup(bar=g.bar, count=0)
    return with_size(plan, 0, groups=(*plan.sizes[0].groups, extra))


def overfull_bar(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    bar = replace(g.bar, stock_length_mm=1000)
    return with_group(plan, 0, 0, replace(g, bar=bar))


def stock_not_offered(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    return with_group(plan, 0, 0, replace(g, bar=replace(g.bar, stock_length_mm=50000)))


def wrong_cut_length(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    cuts = (replace(g.bar.cuts[0], length_mm=g.bar.cuts[0].length_mm - 1), *g.bar.cuts[1:])
    return with_group(plan, 0, 0, replace(g, bar=replace(g.bar, cuts=cuts)))


def unknown_piece(plan: CuttingPlan) -> CuttingPlan:
    g = first_group(plan)
    cuts = (*g.bar.cuts[:-1], Cut(no="999", length_mm=g.bar.cuts[-1].length_mm))
    return with_group(plan, 0, 0, replace(g, bar=replace(g.bar, cuts=cuts)))


def lose_unplaced(plan: CuttingPlan) -> CuttingPlan:
    return with_size(plan, 0, unplaced=())


def fake_unplaced(plan: CuttingPlan) -> CuttingPlan:
    # Claim a placeable piece cannot be placed (and remove it from the bars).
    removed = drop_one_cut(plan)
    lost = first_group(plan).bar.cuts[0]
    fake = Unplaced("DB12", lost.no, lost.length_mm, 1, 12000)
    return with_size(removed, 0, unplaced=(*plan.sizes[0].unplaced, fake))


def wrong_remnants(plan: CuttingPlan) -> CuttingPlan:
    return with_size(plan, 0, remnants=(*plan.sizes[0].remnants, Remnant(5000, 1, 3500)))


def wrong_kerf(plan: CuttingPlan) -> CuttingPlan:
    return with_size(plan, 0, kerf_mm=0)


def bound_above_result(plan: CuttingPlan) -> CuttingPlan:
    return with_size(plan, 0, lower_bound_mm=plan.sizes[0].total_stock_mm + 1)


def missing_size(plan: CuttingPlan) -> CuttingPlan:
    return replace(plan, sizes=plan.sizes[:1])


def bar_filed_under_wrong_size(plan: CuttingPlan) -> CuttingPlan:
    g = plan.sizes[1].groups[0]
    return with_group(plan, 1, 0, replace(g, bar=replace(g.bar, size="DB12")))


@pytest.mark.parametrize(
    "mutate",
    [
        drop_one_cut,
        add_one_bar,
        zero_count_group,
        overfull_bar,
        stock_not_offered,
        wrong_cut_length,
        unknown_piece,
        lose_unplaced,
        fake_unplaced,
        wrong_remnants,
        wrong_kerf,
        bound_above_result,
        missing_size,
        bar_filed_under_wrong_size,
    ],
)
def test_every_mutation_is_caught(good_plan: CuttingPlan, mutate: Mutation) -> None:
    with pytest.raises(InvariantViolation):
        verify_plan(PIECES, mutate(good_plan), SETTINGS)


def test_single_mode_with_two_lengths_is_caught(good_single_plan: CuttingPlan) -> None:
    # Move one bar onto the other stock length, keeping piece counts intact.
    candidates = [
        (size_index, group_index, group, other)
        for size_index, size_plan in enumerate(good_single_plan.sizes)
        for other in (10000, 12000)
        if other != size_plan.single_stock_mm
        for group_index, group in enumerate(size_plan.groups)
        if kerf.fits([c.length_mm for c in group.bar.cuts], other, SINGLE.kerf_mm)
    ]
    assert candidates, "fixture must contain a bar that fits the other stock length"
    size_index, group_index, group, other = candidates[0]
    groups = list(good_single_plan.sizes[size_index].groups)
    groups[group_index] = replace(group, count=group.count - 1)
    moved = BarGroup(replace(group.bar, stock_length_mm=other), 1)
    groups = [g for g in groups if g.count] + [moved]
    mutated = with_size(good_single_plan, size_index, groups=tuple(groups))
    with pytest.raises(InvariantViolation, match="single-length mode"):
        verify_plan(PIECES, mutated, SINGLE)


def test_wrong_single_stock_label_is_caught(good_single_plan: CuttingPlan) -> None:
    mutated = with_size(good_single_plan, 0, single_stock_mm=1)
    with pytest.raises(InvariantViolation, match="chosen length"):
        verify_plan(PIECES, mutated, SINGLE)
