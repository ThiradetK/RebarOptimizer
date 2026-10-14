from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from rebar_optimizer.domain.errors import InputValidationError, Issue, IssueCode
from rebar_optimizer.i18n import th
from rebar_optimizer.io.reader import read_pieces
from tests.conftest import MakeXlsx


def read_errors(path: Path) -> InputValidationError:
    with pytest.raises(InputValidationError) as exc:
        read_pieces(path)
    return exc.value


def codes(issues: tuple[Issue, ...]) -> list[IssueCode]:
    return [i.code for i in issues]


# --- happy path ---------------------------------------------------------------


def test_reads_basic_rows(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx([["DB12", 1, 3.5, 4], ["DB16", 2, 2.1, 3]])
    result = read_pieces(path)
    assert [(p.size, p.no, p.length_mm, p.quantity, p.rows) for p in result.pieces] == [
        ("DB12", "1", 3500, 4, (2,)),
        ("DB16", "2", 2100, 3, (3,)),
    ]
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("metres", "expected_mm"),
    [(3.3, 3300), (3.4, 3400), (0.1, 100), (10, 10000), (2.0006, 2001), ("3.3", 3300)],
)
def test_length_converted_to_integer_mm(
    make_xlsx: MakeXlsx, metres: object, expected_mm: int
) -> None:
    result = read_pieces(make_xlsx([["DB12", 1, metres, 1]]))
    assert result.pieces[0].length_mm == expected_mm
    assert isinstance(result.pieces[0].length_mm, int)


def test_thai_and_messy_headers(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx([["DB12", "A1", 2.5, 2]], headers=(" ขนาด ", "หมายเลข", "ความยาว", "จำนวน"))
    assert read_pieces(path).pieces[0].length_mm == 2500

    path = make_xlsx([[2, 1.5, "DB10", "X"]], headers=("QUANTITY", " length", "SiZe", "no "))
    piece = read_pieces(path).pieces[0]
    assert (piece.size, piece.no, piece.length_mm, piece.quantity) == ("DB10", "X", 1500, 2)


def test_extra_columns_are_kept_in_table(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx(
        [["DB12", 1, 3.5, 4, "ชั้น 2"]], headers=("Size", "No", "Length", "Quantity", "หมายเหตุ")
    )
    table = read_pieces(path).table
    assert table.headers == ("Size", "No", "Length", "Quantity", "หมายเหตุ")
    assert table.rows == (("DB12", 1, 3.5, 4, "ชั้น 2"),)
    assert table.row_numbers == (2,)


# --- normalization (D6) -----------------------------------------------------------


@pytest.mark.parametrize("raw", [12, 12.0, "12", "12.0", " 12 "])
def test_numeric_size_normalized(make_xlsx: MakeXlsx, raw: object) -> None:
    assert read_pieces(make_xlsx([[raw, 1, 1.0, 1]])).pieces[0].size == "12"


@pytest.mark.parametrize("raw", [" db12 ", "DB 12", "db12", "DB12"])
def test_text_size_normalized(make_xlsx: MakeXlsx, raw: str) -> None:
    assert read_pieces(make_xlsx([[raw, 1, 1.0, 1]])).pieces[0].size == "DB12"


@pytest.mark.parametrize(("raw", "expected"), [(5, "5"), (5.0, "5"), (" 5 ", "5"), ("A 1", "A 1")])
def test_no_normalized(make_xlsx: MakeXlsx, raw: object, expected: str) -> None:
    assert read_pieces(make_xlsx([["DB12", raw, 1.0, 1]])).pieces[0].no == expected


def test_size_12_is_not_renamed_but_warned_next_to_db12(make_xlsx: MakeXlsx) -> None:
    result = read_pieces(make_xlsx([[12, 1, 1.0, 1], ["DB12", 2, 1.0, 1]]))
    assert {p.size for p in result.pieces} == {"12", "DB12"}
    assert codes(result.warnings) == [IssueCode.SIZE_AMBIGUOUS_PREFIX]
    assert result.warnings[0].params["similar"] == ["DB12"]


# --- duplicate No rules (D6) -----------------------------------------------------


def test_same_no_different_size_is_error_with_all_rows(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx(
        [["DB12", 5, 1.0, 1], ["DB16", 6, 1.0, 1], ["DB16", 5, 1.0, 1], ["RB9", 5, 2.0, 1]]
    )
    err = read_errors(path)
    assert codes(err.errors) == [IssueCode.DUP_NO_DIFF_SIZE]
    assert err.errors[0].params["rows"] == [2, 4, 5]
    assert "แถว 2, 4, 5" in th.format_issue(err.errors[0])


def test_same_no_same_size_same_length_merges_with_warning(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx([["DB12", 5, 1.5, 2], ["DB12", 7, 1.0, 1], ["DB12", 5.0, "1.5", 3]])
    result = read_pieces(path)
    merged = next(p for p in result.pieces if p.no == "5")
    assert (merged.quantity, merged.rows) == (5, (2, 4))
    assert codes(result.warnings) == [IssueCode.DUP_NO_MERGED]
    assert result.warnings[0].params["rows"] == [2, 4]


def test_same_no_same_size_different_length_is_error(make_xlsx: MakeXlsx) -> None:
    err = read_errors(make_xlsx([["DB12", 5, 1.5, 2], ["", None, None, None], ["DB12", 5, 1.6, 3]]))
    assert codes(err.errors) == [IssueCode.DUP_NO_DIFF_LENGTH]
    assert err.errors[0].params["rows"] == [2, 4]
    message = th.format_issue(err.errors[0])
    assert "แถว 2, 4" in message
    assert "1.500, 1.600" in message


# --- validation problems -------------------------------------------------------


def test_missing_columns(make_xlsx: MakeXlsx) -> None:
    err = read_errors(make_xlsx([["DB12", 3.5]], headers=("Size", "Length")))
    assert codes(err.errors) == [IssueCode.MISSING_COLUMNS]
    assert err.errors[0].params["columns"] == ["No", "Quantity"]
    assert "No (หมายเลข), Quantity (จำนวน)" in th.format_issue(err.errors[0])


def test_duplicate_column_is_error(make_xlsx: MakeXlsx) -> None:
    err = read_errors(
        make_xlsx([["DB12", 1, 1, 1, 2]], headers=("Size", "No", "Length", "Quantity", "จำนวน"))
    )
    assert codes(err.errors) == [IssueCode.DUPLICATE_COLUMN]


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (["DB12", 1, "abc", 1], IssueCode.LENGTH_NOT_NUMBER),
        (["DB12", 1, 0, 1], IssueCode.LENGTH_NOT_POSITIVE),
        (["DB12", 1, -2.5, 1], IssueCode.LENGTH_NOT_POSITIVE),
        (["DB12", 1, 0.0001, 1], IssueCode.LENGTH_ROUNDS_TO_ZERO),
        (["DB12", 1, True, 1], IssueCode.LENGTH_NOT_NUMBER),
        (["DB12", 1, 2.5, 1.5], IssueCode.QUANTITY_INVALID),
        (["DB12", 1, 2.5, 0], IssueCode.QUANTITY_INVALID),
        (["DB12", 1, 2.5, -3], IssueCode.QUANTITY_INVALID),
        (["DB12", 1, 2.5, "สาม"], IssueCode.QUANTITY_INVALID),
        ([None, 1, 2.5, 1], IssueCode.EMPTY_VALUE),
        (["DB12", "  ", 2.5, 1], IssueCode.EMPTY_VALUE),
    ],
)
def test_invalid_cell_values(make_xlsx: MakeXlsx, row: list[object], code: IssueCode) -> None:
    err = read_errors(make_xlsx([["DB12", 99, 1.0, 1], row]))
    assert codes(err.errors) == [code]
    assert err.errors[0].params["row"] == 3
    assert th.format_issue(err.errors[0]).startswith("แถว 3:")


def test_quantity_accepts_integral_float_and_text(make_xlsx: MakeXlsx) -> None:
    result = read_pieces(make_xlsx([["DB12", 1, 1.0, 4.0], ["DB12", 2, 1.0, "7"]]))
    assert [p.quantity for p in result.pieces] == [4, 7]


def test_reports_all_problems_at_once(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx(
        [
            ["DB12", 1, "x", 1],  # row 2
            ["DB12", 2, 1.0, 0],  # row 3
            [None, 3, 1.0, 1],  # row 4
            ["DB12", 4, 1.0, 1],  # row 5 (ok)
            ["DB16", 4, 1.0, 1],  # row 6 (dup No across sizes)
        ]
    )
    err = read_errors(path)
    assert codes(err.errors) == [
        IssueCode.LENGTH_NOT_NUMBER,
        IssueCode.QUANTITY_INVALID,
        IssueCode.EMPTY_VALUE,
        IssueCode.DUP_NO_DIFF_SIZE,
    ]
    assert [e.params["rows" if "rows" in e.params else "row"] for e in err.errors] == [
        2,
        3,
        4,
        [5, 6],
    ]


def test_blank_rows_are_skipped_and_row_numbers_kept(make_xlsx: MakeXlsx) -> None:
    path = make_xlsx(
        [["DB12", 1, 1.0, 1], [None, None, None, None], ["", " ", None, ""], ["DB12", 2, 1.0, 0]]
    )
    err = read_errors(path)
    assert err.errors[0].params["row"] == 5


def test_header_only_file_is_error(make_xlsx: MakeXlsx) -> None:
    assert codes(read_errors(make_xlsx([])).errors) == [IssueCode.NO_DATA]


def test_completely_empty_file_is_error(make_xlsx: MakeXlsx) -> None:
    assert codes(read_errors(make_xlsx([], headers=None)).errors) == [IssueCode.NO_DATA]


# --- warnings --------------------------------------------------------------------


def test_lengths_that_look_like_mm_warn(make_xlsx: MakeXlsx) -> None:
    result = read_pieces(make_xlsx([["DB12", 1, 3500, 1], ["DB12", 2, 100, 1]]))
    assert codes(result.warnings) == [IssueCode.LENGTH_MAYBE_MM]


def test_one_short_length_suppresses_mm_warning(make_xlsx: MakeXlsx) -> None:
    result = read_pieces(make_xlsx([["DB12", 1, 3500, 1], ["DB12", 2, 99.9, 1]]))
    assert result.warnings == ()


def test_sub_mm_precision_warns(make_xlsx: MakeXlsx) -> None:
    result = read_pieces(make_xlsx([["DB12", 1, 3.5004, 1], ["DB12", 2, 3.500005, 1]]))
    assert [p.length_mm for p in result.pieces] == [3500, 3500]
    assert codes(result.warnings) == [IssueCode.LENGTH_SUB_MM]
    assert result.warnings[0].params["row"] == 2


def test_warnings_are_included_alongside_errors(make_xlsx: MakeXlsx) -> None:
    err = read_errors(make_xlsx([["DB12", 1, 3.5004, 1], ["DB12", 2, "x", 1]]))
    assert codes(err.errors) == [IssueCode.LENGTH_NOT_NUMBER]
    assert codes(err.warnings) == [IssueCode.LENGTH_SUB_MM]


# --- formula cells and file-level errors -----------------------------------------


def test_formula_without_cached_value_is_treated_as_empty(tmp_path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Size", "No", "Length", "Quantity"])
    sheet.append(["DB12", 1, "=1+2", 1])  # openpyxl never computes a cached value
    path = tmp_path / "formula.xlsx"
    workbook.save(path)
    err = read_errors(path)
    assert codes(err.errors) == [IssueCode.EMPTY_VALUE]
    assert err.errors[0].params == {"row": 2, "column": "Length"}


def test_missing_file(tmp_path: Path) -> None:
    assert codes(read_errors(tmp_path / "nope.xlsx").errors) == [IssueCode.FILE_NOT_FOUND]


def test_xls_extension_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0")
    err = read_errors(path)
    assert codes(err.errors) == [IssueCode.FILE_NOT_XLSX]
    assert ".xlsx" in th.format_issue(err.errors[0])


def test_corrupt_xlsx_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.xlsx"
    path.write_text("not a zip", encoding="utf-8")
    assert codes(read_errors(path).errors) == [IssueCode.FILE_INVALID]
