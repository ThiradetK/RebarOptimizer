import re

import pytest

import rebar_optimizer
from rebar_optimizer.cli import main


def test_version_is_pep440_like() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", rebar_optimizer.__version__)


def test_cli_version_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert rebar_optimizer.__version__ in capsys.readouterr().out


def test_cli_no_args_returns_zero() -> None:
    assert main([]) == 0


def test_ortools_cp_sat_importable() -> None:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    x = model.new_int_var(0, 10, "x")
    model.maximize(x)
    solver = cp_model.CpSolver()
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(x) == 10


def test_core_packages_do_not_import_qt() -> None:
    import importlib
    import sys

    for name in ("domain", "solver", "services", "io", "i18n"):
        importlib.import_module(f"rebar_optimizer.{name}")
    assert "PySide6" not in sys.modules
