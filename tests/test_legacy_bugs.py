"""Scenarios of the legacy bugs B1–B8 (PLAN.md §2.1), run against the new code only.

Nothing here imports from ``legacy/``: each test rebuilds the *situation* that
triggered a bug and checks the new code handles it.
"""

from __future__ import annotations

from collections import Counter

import pytest

from rebar_optimizer.domain.errors import InputValidationError, InvariantViolation, IssueCode
from rebar_optimizer.domain.models import CuttingPlan, Piece
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.io.reader import read_pieces
from rebar_optimizer.services import planner
from rebar_optimizer.services.invariants import verify_plan
from tests.conftest import MakeXlsx


def cut_counts(plan: CuttingPlan) -> Counter[str]:
    counts: Counter[str] = Counter()
    for size_plan in plan.sizes:
        for group in size_plan.groups:
            for cut in group.bar.cuts:
                counts[cut.no] += group.count
    return counts


def test_b1_pieces_that_do_not_fit_the_current_bar_are_never_dropped() -> None:
    """Legacy hit ``else: break`` and silently lost pieces, which made that combo look cheaper."""
    pieces = [
        Piece("DB12", "1", 7000, 3, (2,)),
        Piece("DB12", "2", 6000, 3, (3,)),
        Piece("DB12", "3", 4500, 5, (4,)),
    ]
    for mode in StockMode:
        result = planner.plan(
            pieces, OptimizerSettings(stock_lengths_mm=(10000, 12000), stock_mode=mode)
        )
        assert cut_counts(result) == {"1": 3, "2": 3, "3": 5}
        assert result.unplaced == ()


def test_b2_short_stock_not_used_when_a_piece_needs_the_long_one() -> None:
    """Legacy only compared against the first stock typed, so with "12,10" the [10] combo
    was evaluated although an 11 m piece cannot come from a 10 m bar."""
    pieces = [Piece("DB12", "1", 11000, 1, (2,)), Piece("DB12", "2", 2500, 8, (3,))]
    settings = OptimizerSettings(stock_lengths_mm=(12000, 10000), stock_mode=StockMode.SINGLE)
    size_plan = planner.plan(pieces, settings).sizes[0]
    assert size_plan.single_stock_mm == 12000
    assert size_plan.unplaced == ()
    assert cut_counts(planner.plan(pieces, settings)) == {"1": 1, "2": 8}


def test_b3_pieces_longer_than_every_stock_are_reported() -> None:
    """Legacy ``oversized_pieces`` always returned {} so such pieces vanished from the output."""
    pieces = [Piece("DB12", "1", 12001, 4, (2,)), Piece("DB12", "2", 3000, 2, (3,))]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(10000, 12000)))
    assert [(u.no, u.length_mm, u.quantity) for u in result.unplaced] == [("1", 12001, 4)]
    assert [i.code for i in result.issues] == [IssueCode.PIECES_UNPLACED]


def test_b4_duplicate_no_never_overwrites_a_length(make_xlsx: MakeXlsx) -> None:
    """Legacy keyed pieces by No alone, so a repeated No silently overwrote its length."""
    path = make_xlsx([["DB12", 5, 3.0, 2], ["DB16", 5, 4.0, 2]])
    with pytest.raises(InputValidationError) as exc:
        read_pieces(path)
    assert [i.code for i in exc.value.errors] == [IssueCode.DUP_NO_DIFF_SIZE]

    path = make_xlsx([["DB12", 5, 3.0, 2], ["DB12", 5, 4.0, 2]])
    with pytest.raises(InputValidationError) as exc:
        read_pieces(path)
    assert [i.code for i in exc.value.errors] == [IssueCode.DUP_NO_DIFF_LENGTH]


def test_b5_stock_lengths_stay_within_the_chosen_set() -> None:
    """Legacy "fixed" bars to lengths outside the chosen combo, including the naturally
    remnant-heavy last bar."""
    pieces = [Piece("DB12", "1", 5000, 4, (2,)), Piece("DB12", "2", 1000, 1, (3,))]
    single = OptimizerSettings(stock_lengths_mm=(6000, 10000), stock_mode=StockMode.SINGLE)
    size_plan = planner.plan(pieces, single).sizes[0]
    assert {g.bar.stock_length_mm for g in size_plan.groups} == {size_plan.single_stock_mm}

    mixed = OptimizerSettings(stock_lengths_mm=(7000, 10000))
    for group in planner.plan(pieces, mixed).sizes[0].groups:
        assert group.bar.stock_length_mm in (7000, 10000)


def test_b6_total_length_beats_bar_count() -> None:
    """Legacy compared bar counts across stock lengths. One 13 m bar holds 6 + 6,
    but two 6 m bars use 1 m less steel."""
    pieces = [Piece("DB12", "1", 6000, 2, (2,))]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(6000, 13000)))
    assert result.sizes[0].total_stock_mm == 12000
    assert result.sizes[0].bar_count == 2


def test_b7_lengths_are_exact_integer_mm(make_xlsx: MakeXlsx) -> None:
    """Legacy used float metres: pieces that exactly fill a bar were rejected.

    (PLAN.md cites 3.3 + 3.3 + 3.4, which happens to be exact in IEEE-754;
    1.5, 4.9, 3.6 on a 10 m bar shows the same trap via "remaining" bookkeeping.)
    """
    remaining = 10.0 - 1.5 - 4.9
    assert remaining < 3.6  # the float trap itself
    path = make_xlsx([["DB12", 1, 1.5, 1], ["DB12", 2, 4.9, 1], ["DB12", 3, 3.6, 1]])
    pieces = read_pieces(path).pieces
    assert [p.length_mm for p in pieces] == [1500, 4900, 3600]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(10000,)))
    assert result.sizes[0].total_stock_mm == 10000


def test_b8_checks_compare_against_independent_data() -> None:
    """Legacy checks compared data with itself and always passed. verify_plan
    recounts from the bars against the *input*, so a lost piece is caught."""
    pieces = [Piece("DB12", "1", 3000, 3, (2,))]
    settings = OptimizerSettings(stock_lengths_mm=(10000,))
    plan = planner.plan(pieces, settings)
    lying_input = [Piece("DB12", "1", 3000, 4, (2,))]
    with pytest.raises(InvariantViolation, match="cut 3"):
        verify_plan(lying_input, plan, settings)
