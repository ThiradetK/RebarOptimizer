"""Checks for the packaging inputs (phase 7). Building the exe itself runs in CI."""

from __future__ import annotations

import io
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import rebar_optimizer
from rebar_optimizer import entry

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def test_selftest_entry_never_imports_qt() -> None:
    code = (
        "import sys\n"
        "from rebar_optimizer.entry import main\n"
        "rc = main(['--selftest'])\n"
        "sys.exit(rc if 'PySide6' not in sys.modules else 99)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_windowed_exe_without_stdout_can_print(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    entry._ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("ok")  # must not raise
    sys.stdout.close()
    sys.stderr.close()


def test_version_resource_matches_package_version() -> None:
    text = (PACKAGING / "version_info.txt").read_text(encoding="utf-8")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", rebar_optimizer.__version__)
    assert match is not None
    major, minor, patch = (int(g) for g in match.groups())
    assert f"filevers=({major}, {minor}, {patch}, 0)" in text
    assert f"prodvers=({major}, {minor}, {patch}, 0)" in text
    assert f"StringStruct('FileVersion', '{major}.{minor}.{patch}')" in text


def test_icon_is_a_multi_size_ico() -> None:
    data = (PACKAGING / "icon.ico").read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    sizes = set()
    stream = io.BytesIO(data[6 : 6 + 16 * count])
    for _ in range(count):
        width = stream.read(16)[0]
        sizes.add(width or 256)
    assert {16, 32, 48, 256} <= sizes


def test_license_texts_are_present() -> None:
    licenses = PACKAGING / "licenses"
    lgpl = (licenses / "LGPL-3.0.txt").read_text(encoding="utf-8")
    gpl = (licenses / "GPL-3.0.txt").read_text(encoding="utf-8")
    assert lgpl.lstrip().startswith("GNU LESSER GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in lgpl
    assert gpl.lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "END OF TERMS AND CONDITIONS" in gpl
    notice = (licenses / "NOTICE.txt").read_text(encoding="utf-8")
    for component in ("PySide6", "OR-Tools", "openpyxl", "onedir"):
        assert component in notice


def test_app_icon_resource_is_packaged() -> None:
    assert (ROOT / "src" / "rebar_optimizer" / "ui" / "resources" / "app.svg").is_file()
    spec = (PACKAGING / "rebar_optimizer.spec").read_text(encoding="utf-8")
    assert "rebar_optimizer/ui/resources" in spec
    assert "console=False" in spec
    assert "exclude_binaries=True" in spec  # onedir (LGPL: Qt DLLs replaceable)
