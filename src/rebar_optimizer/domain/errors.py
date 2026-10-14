"""Error types and issue codes.

Core code never builds user-facing text (D10): problems are recorded as an
``IssueCode`` plus parameters and translated in ``rebar_optimizer.i18n.th``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(StrEnum):
    # --- reader: file level ---
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_NOT_XLSX = "FILE_NOT_XLSX"
    FILE_INVALID = "FILE_INVALID"
    FILE_LOCKED = "FILE_LOCKED"
    MISSING_COLUMNS = "MISSING_COLUMNS"
    DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
    NO_DATA = "NO_DATA"
    # --- reader: row level ---
    EMPTY_VALUE = "EMPTY_VALUE"
    INVALID_TEXT = "INVALID_TEXT"
    LENGTH_NOT_NUMBER = "LENGTH_NOT_NUMBER"
    LENGTH_NOT_POSITIVE = "LENGTH_NOT_POSITIVE"
    LENGTH_ROUNDS_TO_ZERO = "LENGTH_ROUNDS_TO_ZERO"
    LENGTH_SUB_MM = "LENGTH_SUB_MM"
    QUANTITY_INVALID = "QUANTITY_INVALID"
    # --- reader: cross-row ---
    LENGTH_MAYBE_MM = "LENGTH_MAYBE_MM"
    SIZE_AMBIGUOUS_PREFIX = "SIZE_AMBIGUOUS_PREFIX"
    DUP_NO_DIFF_SIZE = "DUP_NO_DIFF_SIZE"
    DUP_NO_DIFF_LENGTH = "DUP_NO_DIFF_LENGTH"
    DUP_NO_MERGED = "DUP_NO_MERGED"
    # --- reader: inventory sheet (phase 8) ---
    INVENTORY_MISSING_COLUMNS = "INVENTORY_MISSING_COLUMNS"
    INVENTORY_SIZE_UNUSED = "INVENTORY_SIZE_UNUSED"
    # --- settings ---
    STOCK_EMPTY = "STOCK_EMPTY"
    STOCK_INVALID_NUMBER = "STOCK_INVALID_NUMBER"
    STOCK_NOT_POSITIVE = "STOCK_NOT_POSITIVE"
    STOCK_DUPLICATE = "STOCK_DUPLICATE"
    KERF_OUT_OF_RANGE = "KERF_OUT_OF_RANGE"
    TIME_LIMIT_NOT_POSITIVE = "TIME_LIMIT_NOT_POSITIVE"
    MAX_PATTERNS_NOT_POSITIVE = "MAX_PATTERNS_NOT_POSITIVE"
    NUM_WORKERS_NOT_POSITIVE = "NUM_WORKERS_NOT_POSITIVE"
    # --- planning ---
    PIECES_UNPLACED = "PIECES_UNPLACED"
    UNPLACED_TOO_LONG = "UNPLACED_TOO_LONG"
    WEIGHT_UNKNOWN = "WEIGHT_UNKNOWN"
    PATTERN_LIMIT_EXCEEDED = "PATTERN_LIMIT_EXCEEDED"
    TIME_LIMIT_HIT = "TIME_LIMIT_HIT"
    # --- output ---
    OUTPUT_LOCKED = "OUTPUT_LOCKED"
    OUTPUT_FAILED = "OUTPUT_FAILED"


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    severity: Severity
    params: Mapping[str, object] = field(default_factory=dict)

    @staticmethod
    def error(code: IssueCode, **params: object) -> Issue:
        return Issue(code, Severity.ERROR, params)

    @staticmethod
    def warning(code: IssueCode, **params: object) -> Issue:
        return Issue(code, Severity.WARNING, params)

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


class RebarOptimizerError(Exception):
    """Base class for all expected (user-reportable) failures."""


class _IssuesError(RebarOptimizerError):
    def __init__(self, issues: Iterable[Issue]) -> None:
        self.issues: tuple[Issue, ...] = tuple(issues)
        super().__init__(", ".join(issue.code for issue in self.issues))

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if i.is_error)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(i for i in self.issues if not i.is_error)


class InputValidationError(_IssuesError):
    """The input workbook cannot be used. ``issues`` holds every problem found."""


class SettingsError(_IssuesError):
    """Optimizer settings are invalid."""


class OutputWriteError(_IssuesError):
    """The output workbook could not be written."""


class InvariantViolation(RebarOptimizerError):
    """A plan failed ``verify_plan()``: a bug, never a user error."""


class Cancelled(RebarOptimizerError):
    """The user cancelled the computation."""
