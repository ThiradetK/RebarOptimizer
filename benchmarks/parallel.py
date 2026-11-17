"""Compare planning sizes one by one with planning them side by side.

python -m benchmarks.parallel wide --time-limit 10 --workers 1 2 4

Per configuration: wall time, average CPU of this process and its workers as
a share of the whole machine, their peak memory, and whether every size ends
with the same totals as the one-by-one run. Needs ``psutil`` (dev extra).
"""

from __future__ import annotations

import argparse
import os
import threading
import time

import psutil

from benchmarks.generate import PRESETS, make_pieces
from rebar_optimizer.domain.models import CuttingPlan, Piece
from rebar_optimizer.domain.settings import OptimizerSettings, PerformanceMode, plan_resources
from rebar_optimizer.services import planner
from rebar_optimizer.services.summary import summarize_size

SAMPLE_S = 0.2


Totals = dict[str, tuple[str, int, int]]
"""``{size: (status, purchased mm, inventory mm)}``: the D2 objective per size."""


def size_totals(plan: CuttingPlan) -> Totals:
    totals: Totals = {}
    for size_plan in plan.sizes:
        summary = summarize_size(size_plan)
        totals[size_plan.size] = (
            str(size_plan.status),
            summary.new_stock_mm,
            summary.inventory_mm,
        )
    return totals


def compare(baseline: Totals, totals: Totals) -> str:
    """Optimal sizes must cost the same; time-limited ones may land either side."""
    notes = []
    for size, (status, new, inventory) in totals.items():
        base_status, base_new, base_inventory = baseline[size]
        if (new, inventory) == (base_new, base_inventory):
            continue
        delta = 100 * (new - base_new) / base_new if base_new else 0.0
        notes.append(f"{size} {delta:+.2f}% ({base_status}->{status})")
    purchased = sum(new for _, new, _ in totals.values())
    base = sum(new for _, new, _ in baseline.values())
    total = f"purchase {100 * (purchased - base) / base:+.3f}%" if base else "purchase +0%"
    return f"{total}; " + ("every size costs the same" if not notes else ", ".join(notes))


def _tree(root: psutil.Process) -> list[psutil.Process]:
    try:
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return [root]


def measure(
    pieces: list[Piece], settings: OptimizerSettings
) -> tuple[CuttingPlan, float, float, float]:
    """``(plan, wall seconds, average CPU % of the machine, peak memory MB)``."""
    result: list[CuttingPlan] = []
    thread = threading.Thread(target=lambda: result.append(planner.plan(pieces, settings)))
    root = psutil.Process()
    cpu_seen: dict[int, float] = {}
    peak = 0.0
    started = time.perf_counter()
    thread.start()
    while thread.is_alive():
        thread.join(SAMPLE_S)
        memory = 0.0
        for process in _tree(root):
            try:
                times = process.cpu_times()
                cpu_seen[process.pid] = times.user + times.system
                memory += process.memory_info().rss
            except psutil.Error:
                continue
        peak = max(peak, memory)
    wall = time.perf_counter() - started
    own_before = cpu_seen.get(root.pid, 0.0) - _OWN_CPU_AT_START[0]
    cpu = own_before + sum(v for pid, v in cpu_seen.items() if pid != root.pid)
    _OWN_CPU_AT_START[0] = cpu_seen.get(root.pid, _OWN_CPU_AT_START[0])
    return result[0], wall, 100 * cpu / wall / (os.cpu_count() or 1), peak / 2**20


_OWN_CPU_AT_START = [0.0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("preset", choices=sorted(PRESETS))
    parser.add_argument("--time-limit", type=float, default=10.0)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument(
        "--mode", choices=[m.value for m in PerformanceMode], default=PerformanceMode.BALANCED.value
    )
    parser.add_argument("--kerf-mm", type=int, default=3)
    parser.add_argument("--threads", type=int, help="CP-SAT threads per size (default: by mode)")
    parser.add_argument(
        "--scale-limit",
        type=float,
        default=1.0,
        help="multiply the time limit by this when sizes run side by side",
    )
    args = parser.parse_args()

    pieces = make_pieces(PRESETS[args.preset])
    times = psutil.Process().cpu_times()
    _OWN_CPU_AT_START[0] = times.user + times.system
    baseline: tuple[Totals, float] | None = None
    print(
        f"{args.preset}: {len({p.size for p in pieces})} sizes, mode {args.mode}, "
        f"{os.cpu_count()} logical CPUs, time limit {args.time_limit:g} s per size"
    )
    print(
        f"{'workers':>7} {'threads':>7} {'wall s':>8} {'speedup':>8} "
        f"{'CPU %':>6} {'RAM MB':>7}  result"
    )
    for workers in args.workers:
        resources = plan_resources(PerformanceMode(args.mode), size_workers=workers)
        settings = OptimizerSettings(
            stock_lengths_mm=(10000, 12000),
            kerf_mm=args.kerf_mm,
            time_limit_s=args.time_limit * (args.scale_limit if workers > 1 else 1.0),
            size_workers=resources.size_workers,
            num_workers=args.threads or resources.num_workers,
        )
        plan, wall, cpu, memory = measure(pieces, settings)
        totals = size_totals(plan)
        if baseline is None:
            baseline = (totals, wall)
            verdict = "baseline"
        else:
            verdict = compare(baseline[0], totals)
        statuses = ",".join(sorted({status for status, _, _ in totals.values()}))
        print(
            f"{resources.size_workers:>7} {settings.num_workers:>7} {wall:>8.1f} "
            f"{baseline[1] / wall:>7.2f}x {cpu:>6.1f} {memory:>7.0f}  {verdict} [{statuses}]"
        )


if __name__ == "__main__":
    main()
