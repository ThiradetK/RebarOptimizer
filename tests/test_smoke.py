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


def test_cli_no_args_asks_for_input() -> None:
    assert main([]) == 1


def test_ortools_cp_sat_importable() -> None:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    x = model.new_int_var(0, 10, "x")
    model.maximize(x)
    solver = cp_model.CpSolver()
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(x) == 10


def test_core_packages_do_not_import_qt() -> None:
    # Run in a fresh interpreter: pytest-qt already imports PySide6 in this process.
    import subprocess
    import sys

    code = (
        "import importlib, sys\n"
        "for name in ('domain', 'solver', 'services', 'io', 'i18n', 'cli'):\n"
        "    importlib.import_module(f'rebar_optimizer.{name}')\n"
        "sys.exit(1 if 'PySide6' in sys.modules else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False)
    assert result.returncode == 0
