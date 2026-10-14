from __future__ import annotations

from collections import Counter

import pytest

from rebar_optimizer.domain.errors import Cancelled
from rebar_optimizer.domain.models import SolveStatus
from rebar_optimizer.services.cancel import CancelToken
from rebar_optimizer.solver import kerf
from rebar_optimizer.solver.base import cut_counts
from rebar_optimizer.solver.greedy import GreedySolver, best_fit_decreasing, expand


def test_expand_longest_first() -> None:
    assert expand({2000: 2, 5000: 1}) == [5000, 2000, 2000]


def test_best_fit_picks_the_tightest_open_bar() -> None:
    # 6 -> bar0 (free 4); 5 -> new bar1 (free 5); 4 -> bar0 (tightest that fits)
    assert best_fit_decreasing([6000, 5000, 4000], 10000, 0)[0] == [[6000, 4000], [5000]]


def test_best_fit_respects_kerf() -> None:
    # 6000 + 4000 + one kerf > 10000, so 4000 cannot join 6000 when k = 1
    assert best_fit_decreasing([6000, 4000], 10000, 1)[0] == [[6000], [4000]]


def test_best_fit_rejects_too_long_piece() -> None:
    with pytest.raises(ValueError, match="longer than stock"):
        best_fit_decreasing([12001], 12000, 0)


def test_every_piece_is_placed_and_bars_fit() -> None:
    demand = Counter({3500: 7, 2100: 9, 1200: 13, 5750: 3})
    result = GreedySolver().solve(demand, (10000, 12000), 3)
    assert result.status is SolveStatus.GREEDY
    assert cut_counts(result.bars) == demand
    for bar in result.bars:
        assert bar.stock_length_mm in (10000, 12000)
        assert kerf.fits(bar.lengths, bar.stock_length_mm, 3)


def test_new_bars_open_long_then_downsize() -> None:
    result = GreedySolver().solve({5000: 1}, (6000, 12000), 0)
    assert [b.stock_length_mm for b in result.bars] == [6000]


def test_single_length_runs_can_beat_longest_first() -> None:
    # Longest-first: 12[6,6] + 10[4,4] = 22 m. Using only 10 m bars: 10[6,4] x 2 = 20 m.
    result = GreedySolver().solve({6000: 2, 4000: 2}, (10000, 12000), 0)
    assert result.total_stock_mm == 20000


def test_empty_demand() -> None:
    assert GreedySolver().solve({}, (10000,), 0).bars == ()


def test_cancel_is_honoured() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        GreedySolver().solve({1000: 1}, (10000,), 0, token)
