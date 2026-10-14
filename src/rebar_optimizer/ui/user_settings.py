"""Remember the last form values and window layout (6.3).

Stored as JSON at ``%APPDATA%\\RebarOptimizer\\settings.json``. A missing,
corrupt or partly invalid file silently falls back to defaults (per field).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from rebar_optimizer.domain.settings import KERF_MAX_MM, KERF_MIN_MM, StockMode

logger = logging.getLogger(__name__)

SETTINGS_ENV = "REBAR_OPTIMIZER_SETTINGS"
APP_DIR_NAME = "RebarOptimizer"
FILE_NAME = "settings.json"
DEFAULT_KERF_MM = 3
"""Pre-filled kerf when the checkbox is ticked (PLAN.md §9 default)."""


@dataclass
class UserSettings:
    stock_text: str = "10, 12"
    stock_mode: str = StockMode.MIXED.value
    kerf_enabled: bool = False
    kerf_mm: int = DEFAULT_KERF_MM
    time_limit_s: float = 30.0
    animations: bool = True
    advanced_open: bool = False
    window_geometry: str = ""
    """Base64 of ``QWidget.saveGeometry()``."""
    splitter_state: str = ""
    last_input_dir: str = ""
    last_output_dir: str = ""


def default_path() -> Path:
    override = os.environ.get(SETTINGS_ENV)
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME / FILE_NAME


def load(path: Path | None = None) -> UserSettings:
    path = path or default_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UserSettings()
    except (OSError, ValueError) as exc:
        logger.warning("ignoring unreadable settings %s: %s", path, exc)
        return UserSettings()
    if not isinstance(raw, dict):
        return UserSettings()
    defaults = UserSettings()
    values: dict[str, object] = {}
    for field in fields(UserSettings):
        default = getattr(defaults, field.name)
        value = raw.get(field.name, default)
        values[field.name] = value if _valid(field.name, value, default) else default
    return UserSettings(**values)  # type: ignore[arg-type]


def _valid(name: str, value: object, default: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
        if ok and name == "kerf_mm":
            return KERF_MIN_MM <= value <= KERF_MAX_MM  # type: ignore[operator]
        return ok
    if isinstance(default, float):
        return isinstance(value, int | float) and not isinstance(value, bool) and value > 0
    if name == "stock_mode":
        return value in {m.value for m in StockMode}
    return isinstance(value, str)


def save(settings: UserSettings, path: Path | None = None) -> None:
    """Best effort: failures are logged, never raised (losing preferences is harmless)."""
    path = path or default_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), "utf-8")
        temp.replace(path)
    except OSError as exc:
        logger.warning("could not save settings to %s: %s", path, exc)
