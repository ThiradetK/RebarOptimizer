"""Command-line entrypoint (6.4).

Exit codes: 0 success, 1 invalid input/settings, 2 some pieces unplaced,
3 any other error.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from rebar_optimizer import __version__
from rebar_optimizer.domain.errors import (
    Cancelled,
    InputValidationError,
    Issue,
    IssueCode,
    OutputWriteError,
    SettingsError,
)
from rebar_optimizer.domain.models import CuttingPlan, InventoryStock, Piece, StockSource
from rebar_optimizer.domain.settings import OptimizerSettings, StockMode, parse_stock_lengths_m
from rebar_optimizer.i18n import th
from rebar_optimizer.i18n.th import CLI_TEXT, format_metres
from rebar_optimizer.io.reader import read_pieces
from rebar_optimizer.io.template import write_template
from rebar_optimizer.io.writer import WriteSource, write_plan
from rebar_optimizer.logging_config import setup_logging
from rebar_optimizer.services import planner
from rebar_optimizer.services.cancel import ProgressEvent, ProgressKind
from rebar_optimizer.services.summary import bar_counts, cut_runs, group_details, summarize_size

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_UNPLACED = 2
EXIT_ERROR = 3

logger = logging.getLogger(__name__)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse would exit with 2, which means "unplaced pieces" here.
        self.print_usage(sys.stderr)
        self.exit(EXIT_INPUT, f"{self.prog}: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="rebar_optimizer", description=CLI_TEXT["description"])
    parser.add_argument("input", nargs="?", type=Path, help=CLI_TEXT["help_input"])
    parser.add_argument("-o", "--output", type=Path, help=CLI_TEXT["help_output"])
    parser.add_argument("--stock", help=CLI_TEXT["help_stock"])
    parser.add_argument("--kerf-mm", type=int, default=0, help=CLI_TEXT["help_kerf"])
    parser.add_argument("--single-length", action="store_true", help=CLI_TEXT["help_single"])
    parser.add_argument("--time-limit", type=float, default=30.0, help=CLI_TEXT["help_time_limit"])
    parser.add_argument(
        "--make-template", type=Path, metavar="PATH", help=CLI_TEXT["help_template"]
    )
    parser.add_argument("--selftest", action="store_true", help=CLI_TEXT["help_selftest"])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _use_utf8_output()
    log_path = setup_logging()
    logger.info("rebar_optimizer %s cli args=%s", __version__, argv)
    try:
        return _run(args, log_path)
    except (InputValidationError, SettingsError) as exc:
        _print_issues(CLI_TEXT["errors_header"], exc.issues)
        return EXIT_INPUT
    except OutputWriteError as exc:
        _print_issues(CLI_TEXT["errors_header"], exc.issues)
        return EXIT_ERROR
    except Cancelled:
        print(CLI_TEXT["cancelled"], file=sys.stderr)
        return EXIT_ERROR
    except Exception:
        logger.exception("unexpected error")
        print(CLI_TEXT["unexpected"].format(log_path=log_path), file=sys.stderr)
        return EXIT_ERROR


def _run(args: argparse.Namespace, log_path: Path) -> int:
    if args.make_template is not None:
        path = write_template(args.make_template)
        print(CLI_TEXT["template_written"].format(path=path))
        return EXIT_OK
    if args.selftest:
        return run_selftest(log_path)
    if args.input is None or args.stock is None:
        print(CLI_TEXT["need_input"], file=sys.stderr)
        return EXIT_INPUT

    settings = OptimizerSettings(
        stock_lengths_mm=parse_stock_lengths_m(args.stock),
        stock_mode=StockMode.SINGLE if args.single_length else StockMode.MIXED,
        kerf_mm=args.kerf_mm,
        time_limit_s=args.time_limit,
    )
    settings.validate()
    result = read_pieces(args.input)
    if result.warnings:
        _print_issues(CLI_TEXT["warnings_header"], result.warnings)

    cutting_plan = planner.plan(
        result.pieces, settings, on_progress=_print_progress, inventory=result.inventory
    )
    for line in format_plan(cutting_plan):
        print(line)
    if args.output is not None:
        source = WriteSource(
            pieces=result.pieces,
            table=result.table,
            input_path=result.path,
            warnings=result.warnings,
            inventory=result.inventory,
            inventory_sheet=result.inventory_sheet,
        )
        written = write_plan(args.output, cutting_plan, source)
        print(CLI_TEXT["output_written"].format(path=written))
    return EXIT_UNPLACED if cutting_plan.unplaced else EXIT_OK


def format_plan(plan: CuttingPlan) -> list[str]:
    """Human-readable Thai summary of ``plan``, one line per item."""
    lines: list[str] = []
    total_bars = total_stock = total_inventory = 0
    for size_plan in plan.sizes:
        if not size_plan.groups:
            continue
        summary = summarize_size(size_plan)
        total_bars += summary.bar_count
        total_stock += summary.stock_mm
        total_inventory += summary.inventory_mm
        lines.append("")
        lines.append(
            CLI_TEXT["size_header"].format(
                size=size_plan.size, status=th.STATUS_LABELS[size_plan.status]
            )
        )
        if size_plan.single_stock_mm is not None:
            lines.append(
                CLI_TEXT["single_stock"].format(stock_m=format_metres(size_plan.single_stock_mm))
            )
        items = ", ".join(
            CLI_TEXT["stock_item"].format(stock_m=format_metres(stock), count=count)
            for stock, count in summary.bars_by_stock.items()
        )
        lines.append(CLI_TEXT["stock_count"].format(items=items or th.NONE_TEXT))
        if summary.inventory_by_stock:
            used = ", ".join(
                CLI_TEXT["stock_item"].format(stock_m=format_metres(stock), count=count)
                for stock, count in summary.inventory_by_stock.items()
            )
            lines.append(CLI_TEXT["inventory_count"].format(items=used))
        lines.append(
            CLI_TEXT["totals"].format(
                stock_m=format_metres(summary.stock_mm),
                pieces_m=format_metres(summary.pieces_mm),
                reusable_m=format_metres(summary.reusable_mm),
                waste_m=format_metres(summary.waste_mm),
                kerf_m=format_metres(summary.kerf_mm),
                utilization=summary.utilization * 100,
            )
        )
        if size_plan.lower_bound_mm is not None and size_plan.gap is not None:
            lines.append(
                CLI_TEXT["bound"].format(
                    bound_m=format_metres(size_plan.lower_bound_mm), gap=size_plan.gap * 100
                )
            )
        details = group_details(size_plan)
        for index, (group, detail) in enumerate(zip(size_plan.groups, details, strict=True), 1):
            tag = (
                CLI_TEXT["reusable_tag"].format(longest_m=format_metres(detail.longest_cuttable_mm))
                if detail.longest_cuttable_mm is not None
                else ""
            )
            lines.append(
                CLI_TEXT["pattern"].format(
                    index=index,
                    stock_m=format_metres(group.bar.stock_length_mm)
                    + (th.INVENTORY_TAG if group.bar.source is StockSource.INVENTORY else ""),
                    count=group.count,
                    cuts=th.format_cuts(cut_runs(group.bar.cuts)),
                    remnant_m=format_metres(detail.usage.remnant_mm),
                    tag=tag,
                )
            )
    lines.append("")
    lines.append(
        CLI_TEXT["grand_total"].format(bars=total_bars, stock_m=format_metres(total_stock))
    )
    if total_inventory:
        lines.append(
            CLI_TEXT["grand_total_split"].format(
                new_m=format_metres(total_stock - total_inventory),
                inventory_m=format_metres(total_inventory),
            )
        )
    counts = bar_counts(plan)
    if counts:
        lines.append("")
        lines.append(th.BAR_COUNT_TITLE + ":")
        lines += [
            "  " + th.format_bar_count(c.size, c.count, c.stock_length_mm, c.inventory)
            for c in counts
        ]
    if plan.unplaced:
        lines.append("")
        lines.append(CLI_TEXT["unplaced_header"])
        for u in plan.unplaced:
            reason = th.format_issue(
                Issue.warning(IssueCode.UNPLACED_TOO_LONG, longest_stock_mm=u.longest_stock_mm)
            )
            lines.append(
                CLI_TEXT["unplaced_item"].format(
                    size=u.size,
                    no=u.no,
                    length_m=format_metres(u.length_mm),
                    quantity=u.quantity,
                    reason=reason,
                )
            )
    return lines


def _print_progress(event: ProgressEvent) -> None:
    if event.kind is ProgressKind.SIZE_STARTED:
        print(
            CLI_TEXT["progress"].format(index=event.index + 1, total=event.total, size=event.size),
            file=sys.stderr,
        )
    elif event.kind is ProgressKind.STAGE and event.stage is not None:
        print(
            CLI_TEXT["progress_stage"].format(
                stage=th.STAGE_LABELS[event.stage], step=th.stage_step_text(event.step, event.steps)
            ),
            file=sys.stderr,
        )


def _print_issues(header: str, issues: Sequence[Issue]) -> None:
    print(header, file=sys.stderr)
    for line in th.format_issues(issues):
        print(f"  {line}", file=sys.stderr)


def _use_utf8_output() -> None:
    """Thai text must survive redirection to a file or pipe on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


# --- selftest ----------------------------------------------------------------------

SELFTEST_PIECES: tuple[Piece, ...] = (
    Piece("DB12", "1", 1500, 1, (2,)),
    Piece("DB12", "2", 4900, 1, (3,)),
    Piece("DB12", "3", 3600, 1, (4,)),
    Piece("DB16", "4", 3500, 7, (5,)),
    Piece("DB16", "5", 2100, 9, (6,)),
    Piece("DB16", "6", 12500, 1, (7,)),
)


SELFTEST_INVENTORY: tuple[InventoryStock, ...] = (InventoryStock("DB16", 7000, 2, (2,)),)


def run_selftest(log_path: Path) -> int:
    """Solve a built-in problem in every mode; ``verify_plan`` runs inside ``plan``."""
    try:
        exact = planner.plan(SELFTEST_PIECES[:3], OptimizerSettings(stock_lengths_mm=(10000,)))
        if exact.sizes[0].total_stock_mm != 10000:
            raise AssertionError("1.5 + 4.9 + 3.6 must fill exactly one 10 m bar")
        for mode in StockMode:
            for kerf_mm in (0, 3):
                settings = OptimizerSettings(
                    stock_lengths_mm=(10000, 12000), stock_mode=mode, kerf_mm=kerf_mm
                )
                result = planner.plan(SELFTEST_PIECES, settings)
                if len(result.unplaced) != 1:
                    raise AssertionError("the 12.5 m piece must be reported as unplaced")
                stocked = planner.plan(SELFTEST_PIECES, settings, inventory=SELFTEST_INVENTORY)
                if stocked.sizes[1].inventory_stock_mm == 0:
                    raise AssertionError("the 7 m inventory bars of DB16 must be used")
    except Exception:
        logger.exception("selftest failed")
        print(CLI_TEXT["selftest_failed"].format(log_path=log_path), file=sys.stderr)
        return EXIT_ERROR
    logger.info("selftest passed")
    print(CLI_TEXT["selftest_ok"])
    return EXIT_OK
