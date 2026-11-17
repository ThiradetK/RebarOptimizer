from __future__ import annotations

from pathlib import Path

import pytest

from rebar_optimizer.cli import EXIT_ERROR, EXIT_INPUT, EXIT_OK, EXIT_UNPLACED, main
from rebar_optimizer.io.template import write_template
from tests.conftest import MakeXlsx

Capture = pytest.CaptureFixture[str]


def test_template_plan_prints_every_size(tmp_path: Path, capsys: Capture) -> None:
    path = write_template(tmp_path / "t.xlsx")
    assert main([str(path), "--stock", "10,12"]) == EXIT_OK
    out = capsys.readouterr().out
    for size in ("DB12", "DB16", "RB9"):
        assert f"Size {size}" in out
    assert "รวมทุก Size" in out
    assert "แบบที่ 1:" in out


def test_single_length_and_kerf_options(tmp_path: Path, capsys: Capture) -> None:
    path = write_template(tmp_path / "t.xlsx")
    code = main([str(path), "--stock", "10,12", "--single-length", "--kerf-mm", "3"])
    assert code == EXIT_OK
    assert "ความยาวเหล็กเส้นที่เลือก" in capsys.readouterr().out


def test_unplaced_pieces_exit_2_but_still_report(make_xlsx: MakeXlsx, capsys: Capture) -> None:
    path = make_xlsx([["DB12", 1, 13.0, 2], ["DB12", 2, 3.0, 1]])
    assert main([str(path), "--stock", "10,12"]) == EXIT_UNPLACED
    out = capsys.readouterr().out
    assert "ชิ้นงานที่วางไม่ได้" in out
    assert "No 1 ยาว 13.000 ม. × 2 ชิ้น" in out


def test_input_errors_exit_1_with_row_numbers(make_xlsx: MakeXlsx, capsys: Capture) -> None:
    path = make_xlsx([["DB12", 1, "x", 1]])
    assert main([str(path), "--stock", "10"]) == EXIT_INPUT
    assert "แถว 2:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [["--stock", "10,abc"], ["--stock", "10", "--kerf-mm", "50"], ["--stock", "0"]],
)
def test_settings_errors_exit_1(make_xlsx: MakeXlsx, args: list[str], capsys: Capture) -> None:
    path = make_xlsx([["DB12", 1, 1.0, 1]])
    assert main([str(path), *args]) == EXIT_INPUT
    assert "พบปัญหา" in capsys.readouterr().err


def test_bad_arguments_exit_1_not_2(capsys: Capture) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["x.xlsx", "--kerf-mm", "abc"])
    assert exc.value.code == EXIT_INPUT


def test_missing_stock_exit_1(make_xlsx: MakeXlsx) -> None:
    assert main([str(make_xlsx([["DB12", 1, 1.0, 1]]))]) == EXIT_INPUT


def test_make_template(tmp_path: Path, capsys: Capture) -> None:
    target = tmp_path / "template.xlsx"
    assert main(["--make-template", str(target)]) == EXIT_OK
    assert target.is_file()
    assert str(target) in capsys.readouterr().out


def test_make_template_into_missing_folder_exit_3(tmp_path: Path) -> None:
    assert main(["--make-template", str(tmp_path / "nope" / "t.xlsx")]) == EXIT_ERROR


def test_selftest(capsys: Capture) -> None:
    assert main(["--selftest"]) == EXIT_OK
    assert "selftest ผ่าน" in capsys.readouterr().out


def test_unexpected_error_exit_3_and_logged(
    make_xlsx: MakeXlsx, monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("rebar_optimizer.services.planner.plan", boom)
    assert main([str(make_xlsx([["DB12", 1, 1.0, 1]])), "--stock", "10"]) == EXIT_ERROR
    assert "log" in capsys.readouterr().err


def test_bar_counts_per_size_and_length(tmp_path: Path, capsys: Capture) -> None:
    path = write_template(tmp_path / "t.xlsx")
    assert main([str(path), "--stock", "10,12"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "สรุปจำนวนเส้นแยกตาม Size และความยาว:" in out
    lines = [line.strip() for line in out.splitlines() if line.strip().startswith("Size ")]
    counted = [line for line in lines if "จำนวน" in line and line.endswith("เมตร")]
    assert counted, out
    assert all(line.split()[1] in {"DB12", "DB16", "RB9"} for line in counted)


def test_workers_option_plans_sizes_side_by_side(tmp_path: Path, capsys: Capture) -> None:
    def stock_totals(out: str) -> list[str]:
        # Equally good plans may arrange bars differently (D2): compare what is bought.
        headers = [line for line in out.splitlines() if line.startswith("Size ") and "—" in line]
        totals = [line.split("|")[0] for line in out.splitlines() if "เหล็กรวม" in line]
        return headers + totals

    path = write_template(tmp_path / "t.xlsx")
    assert main([str(path), "--stock", "10,12"]) == EXIT_OK
    sequential = stock_totals(capsys.readouterr().out)
    assert main([str(path), "--stock", "10,12", "--workers", "2", "--mode", "eco"]) == EXIT_OK
    captured = capsys.readouterr()
    assert sequential and stock_totals(captured.out) == sequential
    assert "Size DB16: " in captured.err  # stage lines name their size


def test_workers_must_be_positive(tmp_path: Path, capsys: Capture) -> None:
    path = write_template(tmp_path / "t.xlsx")
    assert main([str(path), "--stock", "10", "--workers", "0"]) == EXIT_INPUT
    assert "จำนวน worker" in capsys.readouterr().err
