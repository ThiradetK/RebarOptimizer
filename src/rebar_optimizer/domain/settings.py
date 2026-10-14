"""Optimizer settings shared by CLI and GUI."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from enum import StrEnum

from rebar_optimizer.domain.errors import Issue, IssueCode, SettingsError
from rebar_optimizer.domain.models import StockOption

KERF_MIN_MM = 0
KERF_MAX_MM = 20


class StockMode(StrEnum):
    MIXED = "mixed"
    """Solver may mix all stock lengths freely (default)."""
    SINGLE = "single"
    """Each size uses exactly one stock length (D4)."""


def default_num_workers() -> int:
    return min(8, os.cpu_count() or 1)


@dataclass(frozen=True)
class OptimizerSettings:
    stock_lengths_mm: tuple[int, ...]
    stock_mode: StockMode = StockMode.MIXED
    kerf_mm: int = 0
    time_limit_s: float = 30.0
    max_patterns: int = 50_000
    random_seed: int = 0
    num_workers: int = field(default_factory=default_num_workers)

    def validate(self) -> None:
        """Raise ``SettingsError`` listing every invalid setting."""
        issues: list[Issue] = []
        if not self.stock_lengths_mm:
            issues.append(Issue.error(IssueCode.STOCK_EMPTY))
        seen: set[int] = set()
        for length in self.stock_lengths_mm:
            if length <= 0:
                issues.append(Issue.error(IssueCode.STOCK_NOT_POSITIVE, length_mm=length))
            elif length in seen:
                issues.append(Issue.error(IssueCode.STOCK_DUPLICATE, length_mm=length))
            seen.add(length)
        if not KERF_MIN_MM <= self.kerf_mm <= KERF_MAX_MM:
            issues.append(
                Issue.error(
                    IssueCode.KERF_OUT_OF_RANGE,
                    kerf_mm=self.kerf_mm,
                    min_mm=KERF_MIN_MM,
                    max_mm=KERF_MAX_MM,
                )
            )
        if not (math.isfinite(self.time_limit_s) and self.time_limit_s > 0):
            issues.append(Issue.error(IssueCode.TIME_LIMIT_NOT_POSITIVE, value=self.time_limit_s))
        if self.max_patterns <= 0:
            issues.append(Issue.error(IssueCode.MAX_PATTERNS_NOT_POSITIVE, value=self.max_patterns))
        if self.num_workers <= 0:
            issues.append(Issue.error(IssueCode.NUM_WORKERS_NOT_POSITIVE, value=self.num_workers))
        if issues:
            raise SettingsError(issues)

    @property
    def stock_options(self) -> tuple[StockOption, ...]:
        """Stock lengths as unlimited new stock, shortest first."""
        return tuple(StockOption(length) for length in sorted(self.stock_lengths_mm))


def parse_stock_lengths_m(text: str) -> tuple[int, ...]:
    """Parse ``"10, 12"`` (metres, comma separated) into millimetres.

    Duplicates are dropped (first occurrence kept); empty items are ignored.
    Non-positive values are kept so that ``validate()`` reports them.
    Raises ``SettingsError`` if any item is not a number.
    """
    issues: list[Issue] = []
    result: list[int] = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            metres = float(item)
        except ValueError:
            metres = math.nan
        if not math.isfinite(metres):
            issues.append(Issue.error(IssueCode.STOCK_INVALID_NUMBER, text=item))
            continue
        length_mm = round(metres * 1000)
        if length_mm not in result:
            result.append(length_mm)
    if issues:
        raise SettingsError(issues)
    return tuple(result)
