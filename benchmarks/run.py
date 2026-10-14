"""Compare greedy with the full planner (greedy + ILP) on synthetic data.

    python -m benchmarks.run medium --time-limit 30
"""

from __future__ import annotations

import argparse
import time
from collections import Counter

from benchmarks.generate import PRESETS, make_pieces
from rebar_optimizer.domain.models import Piece
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode
from rebar_optimizer.services import planner
from rebar_optimizer.services.postprocess import postprocess
from rebar_optimizer.solver.base import total_stock_mm
from rebar_optimizer.solver.greedy import GreedySolver


def greedy_total(pieces: list[Piece], settings: OptimizerSettings) -> int:
    demand: Counter[int] = Counter()
    for p in pieces:
        demand[p.length_mm] += p.quantity
    stocks = tuple(sorted(settings.stock_lengths_mm))
    sets = (
        [(s,) for s in stocks if s >= max(demand)]
        if settings.stock_mode is StockMode.SINGLE
        else [stocks]
    )
    totals = []
    for stock_set in sets:
        bars = GreedySolver().solve(demand, stock_set, settings.kerf_mm).bars
        totals.append(total_stock_mm(postprocess(bars, demand, stock_set, settings.kerf_mm)))
    return min(totals)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("preset", choices=sorted(PRESETS))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--stock", default="10,12")
    parser.add_argument("--kerf-mm", type=int, default=0)
    parser.add_argument("--single-length", action="store_true")
    parser.add_argument("--time-limit", type=float, default=30.0)
    args = parser.parse_args()

    pieces = make_pieces(PRESETS[args.preset], args.seed)
    settings = OptimizerSettings(
        stock_lengths_mm=tuple(round(float(s) * 1000) for s in args.stock.split(",")),
        stock_mode=StockMode.SINGLE if args.single_length else StockMode.MIXED,
        kerf_mm=args.kerf_mm,
        time_limit_s=args.time_limit,
    )
    print(
        f"preset={args.preset} seed={args.seed} stock={args.stock} kerf={args.kerf_mm} "
        f"mode={settings.stock_mode} time_limit={args.time_limit}s pieces="
        f"{sum(p.quantity for p in pieces)}"
    )
    print()
    print("| Size | ชิ้น | ความยาว | Greedy (m) | ILP (m) | ลดลง | สถานะ | gap | เวลา (s) |")
    print("|---|---:|---:|---:|---:|---:|---|---:|---:|")
    sums = [0, 0]
    for size in dict.fromkeys(p.size for p in pieces):
        size_pieces = [p for p in pieces if p.size == size]
        greedy = greedy_total(size_pieces, settings)
        started = time.perf_counter()
        size_plan = planner.plan(size_pieces, settings).sizes[0]
        elapsed = time.perf_counter() - started
        final = size_plan.total_stock_mm
        sums[0] += greedy
        sums[1] += final
        gap = size_plan.gap or 0.0
        print(
            f"| {size} | {sum(p.quantity for p in size_pieces)} | {len(size_pieces)} "
            f"| {greedy / 1000:.1f} | {final / 1000:.1f} | {(greedy - final) / greedy:.1%} "
            f"| {size_plan.status} | {gap:.2%} | {elapsed:.1f} |"
        )
    print(
        f"| **รวม** | | | {sums[0] / 1000:.1f} | {sums[1] / 1000:.1f} "
        f"| {(sums[0] - sums[1]) / sums[0]:.1%} | | | |"
    )


if __name__ == "__main__":
    main()
