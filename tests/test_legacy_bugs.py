"""Scenarios of the legacy bugs B1–B8 (PLAN.md §2.1), run against the new code only.

Phase 1 covers the reader side of B4 and B7; solver-side scenarios arrive in phase 2.
"""

from __future__ import annotations

import pytest

from rebar_optimizer.domain.errors import InputValidationError, IssueCode
from rebar_optimizer.io.reader import read_pieces
from tests.conftest import MakeXlsx


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


def test_b7_lengths_are_exact_integer_mm(make_xlsx: MakeXlsx) -> None:
    """Legacy used float metres: pieces that exactly fill a bar were rejected.

    (PLAN.md cites 3.3 + 3.3 + 3.4, which happens to be exact in IEEE-754;
    1.5, 4.9, 3.6 on a 10 m bar shows the same trap via "remaining" bookkeeping.)
    """
    remaining = 10.0 - 1.5 - 4.9
    assert remaining < 3.6  # the float trap itself
    result = read_pieces(make_xlsx([["DB12", 1, 1.5, 1], ["DB12", 2, 4.9, 1], ["DB12", 3, 3.6, 1]]))
    lengths = [p.length_mm for p in result.pieces]
    assert lengths == [1500, 4900, 3600]
    assert 10_000 - lengths[0] - lengths[1] == lengths[2]
