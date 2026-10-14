from __future__ import annotations

from pathlib import Path

import pytest

from rebar_optimizer.domain.errors import IssueCode, OutputWriteError
from rebar_optimizer.io.reader import read_pieces
from rebar_optimizer.io.template import SAMPLE_ROWS, write_template


def test_template_reads_back_cleanly(tmp_path: Path) -> None:
    path = write_template(tmp_path / "template.xlsx")
    result = read_pieces(path)
    assert result.warnings == ()
    assert [(p.size, p.no, p.length_mm, p.quantity) for p in result.pieces] == [
        (size, no, round(length * 1000), qty) for size, no, length, qty in SAMPLE_ROWS
    ]


def test_template_into_missing_folder_raises_output_error(tmp_path: Path) -> None:
    with pytest.raises(OutputWriteError) as exc:
        write_template(tmp_path / "no_such_dir" / "template.xlsx")
    assert exc.value.issues[0].code is IssueCode.OUTPUT_FAILED
