# PyInstaller spec: onedir, windowed (PLAN.md phase 7).
#
#     pyinstaller --noconfirm --clean packaging/rebar_optimizer.spec
#
# Output: dist/RebarOptimizer/RebarOptimizer.exe (+ _internal/ and licenses/).
# -*- mode: python ; coding: utf-8 -*-

import shutil
import sys
from importlib import metadata
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - provided by PyInstaller
SRC = ROOT / "src"
PACKAGING = ROOT / "packaging"
NAME = "RebarOptimizer"

# Distributions shipped in the bundle whose license files are copied to licenses/.
LICENSED_DISTRIBUTIONS = [
    "PySide6-Essentials",
    "shiboken6",
    "ortools",
    "openpyxl",
    "et-xmlfile",
    "numpy",
    "pandas",
    "protobuf",
    "absl-py",
    "immutabledict",
    "python-dateutil",
    "six",
    "tzdata",
]

# Qt modules the app never imports (keeps the zip small; PLAN.md phase 7).
QT_EXCLUDES = [
    f"PySide6.{name}"
    for name in (
        "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
        "QtBluetooth", "QtCharts", "QtConcurrent", "QtDataVisualization", "QtDesigner",
        "QtGraphs", "QtHelp", "QtHttpServer", "QtLocation", "QtMultimedia",
        "QtMultimediaWidgets", "QtNetwork", "QtNetworkAuth", "QtNfc", "QtOpenGL",
        "QtOpenGLWidgets", "QtPdf", "QtPdfWidgets", "QtPositioning", "QtQml", "QtQuick",
        "QtQuick3D", "QtQuickControls2", "QtQuickWidgets", "QtRemoteObjects", "QtScxml",
        "QtSensors", "QtSerialBus", "QtSerialPort", "QtSpatialAudio", "QtSql",
        "QtStateMachine", "QtSvgWidgets", "QtTest", "QtTextToSpeech", "QtUiTools",
        "QtWebChannel", "QtWebEngineCore", "QtWebEngineQuick", "QtWebEngineWidgets",
        "QtWebSockets", "QtWebView", "QtXml",
    )
]
PY_EXCLUDES = [
    "tkinter", "matplotlib", "IPython", "pytest", "hypothesis", "PyInstaller",
    "mypy", "ruff", "setuptools", "pip", "benchmarks", "tests",
]
KEEP_TRANSLATIONS = ("_th", "_en")

ortools_datas, ortools_binaries, ortools_hidden = collect_all("ortools")

a = Analysis(  # noqa: F821
    [str(PACKAGING / "launcher.py")],
    pathex=[str(SRC)],
    binaries=ortools_binaries,
    datas=ortools_datas
    + [(str(SRC / "rebar_optimizer" / "ui" / "resources"), "rebar_optimizer/ui/resources")],
    hiddenimports=ortools_hidden,
    excludes=QT_EXCLUDES + PY_EXCLUDES,
    noarchive=False,
)


def _is_unused_translation(dest: str) -> bool:
    path = dest.replace("\\", "/")
    if "/translations/" not in path or "PySide6" not in path:
        return False
    return not any(tag in Path(path).stem for tag in KEEP_TRANSLATIONS)


a.datas = [entry for entry in a.datas if not _is_unused_translation(entry[0])]
# Software OpenGL fallback (~20 MB); a Qt Widgets app never needs it.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() != "opengl32sw.dll"]

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    icon=str(PACKAGING / "icon.ico"),
    version=str(PACKAGING / "version_info.txt"),
    upx=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)


# --- licenses/ next to the exe ---------------------------------------------------------


def _copy_licenses(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PACKAGING / "licenses", target)
    versions = []
    for dist_name in LICENSED_DISTRIBUTIONS:
        dist = metadata.distribution(dist_name)
        versions.append(f"{dist.metadata['Name']} {dist.version}")
        for file in dist.files or []:
            name = Path(str(file)).name.lower()
            if any(key in name for key in ("license", "licence", "copying", "notice")):
                source = Path(dist.locate_file(file))
                if source.is_file():
                    destination = target / dist.metadata["Name"] / source.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, target / "Python-LICENSE.txt")
    from PySide6.QtCore import qVersion  # noqa: PLC0415

    versions.append(f"Qt {qVersion()}")
    versions.append(f"Python {sys.version.split()[0]}")
    (target / "versions.txt").write_text("\n".join(versions) + "\n", encoding="utf-8")


_copy_licenses(Path(DISTPATH) / NAME / "licenses")  # noqa: F821
