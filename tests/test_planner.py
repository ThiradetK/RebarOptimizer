from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rebar_optimizer.domain.errors import Cancelled, Issue, IssueCode, SettingsError
from rebar_optimizer.domain.models import Piece
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.i18n import th
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import CancelToken, ProgressEvent, ProgressKind
from rebar_optimizer.services.summary import summarize_size


def piece(size: str, no: str, length_mm: int, quantity: int, row: int = 2) -> Piece:
    return Piece(size, no, length_mm, quantity, (row,))


def test_natural_key_orders_numbers_numerically() -> None:
    assert sorted(["10", "2", "A1", "1"], key=planner.natural_key) == ["1", "2", "10", "A1"]
    assert sorted(["B10", "B2"], key=planner.natural_key) == ["B2", "B10"]


def test_sizes_keep_input_order() -> None:
    pieces = [piece("DB16", "1", 1000, 1), piece("DB12", "2", 1000, 1)]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    assert [s.size for s in result.sizes] == ["DB16", "DB12"]


def test_identical_bars_are_grouped_with_the_same_numbers() -> None:
    pieces = [piece("DB12", "1", 6000, 10), piece("DB12", "2", 6000, 10)]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    groups = result.sizes[0].groups
    assert [(g.count, [c.no for c in g.bar.cuts]) for g in groups] == [
        (5, ["1", "1"]),
        (5, ["2", "2"]),
    ]


def test_cuts_sorted_longest_first() -> None:
    pieces = [piece("DB12", "1", 1000, 1), piece("DB12", "2", 5000, 1)]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(12000,)))
    assert [c.length_mm for c in result.sizes[0].groups[0].bar.cuts] == [5000, 1000]


def test_oversize_pieces_are_unplaced_with_warning() -> None:
    pieces = [piece("DB12", "1", 12500, 2), piece("DB12", "2", 3000, 3)]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(10000, 12000)))
    assert [(u.no, u.quantity, u.longest_stock_mm) for u in result.unplaced] == [("1", 2, 12000)]
    assert [i.code for i in result.issues] == [IssueCode.PIECES_UNPLACED]
    assert sum(c.length_mm == 3000 for g in result.sizes[0].groups for c in g.bar.cuts) > 0


def test_size_with_only_unplaced_pieces() -> None:
    result = planner.plan(
        [piece("DB25", "1", 15000, 1)], OptimizerSettings(stock_lengths_mm=(12000,))
    )
    assert result.sizes[0].groups == ()
    assert result.worst_status is None


# --- single-length mode (D4) ----------------------------------------------------


def test_single_mode_ignores_pieces_longer_than_every_stock() -> None:
    pieces = [piece("DB12", "1", 13000, 1), piece("DB12", "2", 5000, 2)]
    settings = OptimizerSettings(stock_lengths_mm=(10000, 12000), stock_mode=StockMode.SINGLE)
    result = planner.plan(pieces, settings)
    size_plan = result.sizes[0]
    assert [u.no for u in size_plan.unplaced] == ["1"]
    assert size_plan.single_stock_mm == 10000
    assert {g.bar.stock_length_mm for g in size_plan.groups} == {10000}


def test_single_mode_only_uses_lengths_holding_the_longest_piece() -> None:
    # 10 m alone would be cheaper for the 3 m pieces, but the 11 m piece needs 12 m.
    pieces = [piece("DB12", "1", 11000, 1), piece("DB12", "2", 3000, 6)]
    settings = OptimizerSettings(stock_lengths_mm=(10000, 12000), stock_mode=StockMode.SINGLE)
    size_plan = planner.plan(pieces, settings).sizes[0]
    assert size_plan.single_stock_mm == 12000
    assert size_plan.unplaced == ()


def test_single_mode_picks_least_total_length_per_size() -> None:
    pieces = [piece("A", "1", 5000, 4), piece("B", "2", 6000, 4)]
    settings = OptimizerSettings(stock_lengths_mm=(10000, 12000), stock_mode=StockMode.SINGLE)
    result = planner.plan(pieces, settings)
    assert [(s.size, s.single_stock_mm, s.total_stock_mm) for s in result.sizes] == [
        ("A", 10000, 20000),
        ("B", 12000, 24000),
    ]


def test_mixed_mode_can_mix_lengths() -> None:
    pieces = [piece("A", "1", 6000, 2), piece("A", "2", 3500, 1)]
    result = planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(4000, 12000)))
    assert summarize_size(result.sizes[0]).bars_by_stock == {4000: 1, 12000: 1}


# --- status, bounds, progress, cancel ----------------------------------------------


def test_lower_bound_never_exceeds_result() -> None:
    pieces = [piece("A", "1", 3300, 7), piece("A", "2", 2100, 5)]
    for mode in StockMode:
        settings = OptimizerSettings(stock_lengths_mm=(10000, 12000), stock_mode=mode, kerf_mm=3)
        size_plan = planner.plan(pieces, settings).sizes[0]
        assert size_plan.lower_bound_mm is not None
        assert size_plan.lower_bound_mm <= size_plan.total_stock_mm
        assert size_plan.gap is not None and 0 <= size_plan.gap < 1


def test_simple_lower_bound_single_length_counts_kerf() -> None:
    # 4 x (2500 + 5) = 10020 > 10005 capacity -> at least 2 bars
    assert planner.simple_lower_bound({2500: 4}, (10000,), 5) == 20000
    assert planner.simple_lower_bound({2500: 4}, (10000,), 0) == 10000


def test_progress_events_in_order() -> None:
    events: list[ProgressEvent] = []
    pieces = [piece("A", "1", 1000, 1), piece("B", "2", 1000, 1)]
    planner.plan(pieces, OptimizerSettings(stock_lengths_mm=(12000,)), on_progress=events.append)
    assert [(e.kind, e.size, e.index, e.total) for e in events] == [
        (ProgressKind.SIZE_STARTED, "A", 0, 2),
        (ProgressKind.SIZE_FINISHED, "A", 0, 2),
        (ProgressKind.SIZE_STARTED, "B", 1, 2),
        (ProgressKind.SIZE_FINISHED, "B", 1, 2),
    ]
    assert [e.fraction for e in events] == [0.0, 0.5, 0.5, 1.0]


def test_cancel_midway_raises() -> None:
    token = CancelToken()
    seen: list[str] = []

    def on_progress(event: ProgressEvent) -> None:
        seen.append(event.size)
        if event.kind is ProgressKind.SIZE_FINISHED:
            token.cancel()

    pieces = [piece("A", "1", 1000, 1), piece("B", "2", 1000, 1)]
    with pytest.raises(Cancelled):
        planner.plan(
            pieces, OptimizerSettings(stock_lengths_mm=(12000,)), on_progress, cancel=token
        )
    assert seen == ["A", "A"]


def test_invalid_settings_rejected() -> None:
    with pytest.raises(SettingsError):
        planner.plan([piece("A", "1", 1000, 1)], OptimizerSettings(stock_lengths_mm=()))


# --- property: verify_plan always passes ---------------------------------------------

piece_lists = st.lists(
    st.tuples(
        st.sampled_from(["DB12", "DB16"]),
        st.integers(100, 13000).map(lambda x: x - x % 50),
        st.integers(1, 6),
    ),
    min_size=1,
    max_size=8,
)


@settings(max_examples=80, deadline=None)
@given(
    rows=piece_lists,
    stocks=st.sets(st.sampled_from([6000, 9000, 10000, 12000]), min_size=1, max_size=3),
    kerf_mm=st.sampled_from([0, 3, 7]),
    mode=st.sampled_from(list(StockMode)),
)
def test_plans_always_verify(
    rows: list[tuple[str, int, int]], stocks: set[int], kerf_mm: int, mode: StockMode
) -> None:
    pieces = [
        piece(size, str(i), max(length, 50), qty) for i, (size, length, qty) in enumerate(rows)
    ]
    settings = OptimizerSettings(stock_lengths_mm=tuple(stocks), stock_mode=mode, kerf_mm=kerf_mm)
    result = planner.plan(pieces, settings)  # verify_plan runs inside
    cut = Counter[str]()
    for size_plan in result.sizes:
        for group in size_plan.groups:
            for c in group.bar.cuts:
                cut[c.no] += group.count
    for p in pieces:
        unplaced = sum(u.quantity for u in result.unplaced if u.no == p.no)
        assert cut[p.no] + unplaced == p.quantity


def test_same_warning_for_several_sizes_is_merged() -> None:
    issues = [
        Issue.warning(IssueCode.PATTERN_LIMIT_EXCEEDED, size="DB12", max_patterns=5),
        Issue.warning(IssueCode.TIME_LIMIT_HIT, size="DB12", seconds=1.0),
        Issue.warning(IssueCode.PATTERN_LIMIT_EXCEEDED, size="DB16", max_patterns=5),
        Issue.warning(IssueCode.TIME_LIMIT_HIT, size="DB20", seconds=0.5),
    ]
    merged = planner.merge_size_issues(issues)
    assert [i.code for i in merged] == [
        IssueCode.PATTERN_LIMIT_EXCEEDED,
        IssueCode.TIME_LIMIT_HIT,
        IssueCode.TIME_LIMIT_HIT,
    ]
    assert merged[0].params["size"] == ["DB12", "DB16"]
    assert th.format_issue(merged[0]).startswith("Size DB12, DB16:")
    assert merged[1].params["size"] == "DB12"  # different seconds: not merged
