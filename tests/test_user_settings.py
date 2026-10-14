from __future__ import annotations

import json
from pathlib import Path

from rebar_optimizer.ui import user_settings
from rebar_optimizer.ui.user_settings import UserSettings


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert user_settings.load(tmp_path / "none.json") == UserSettings()


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "settings.json"
    original = UserSettings(
        stock_text="6, 9, 12",
        stock_mode="single",
        kerf_enabled=True,
        kerf_mm=5,
        time_limit_s=45.0,
        animations=False,
        window_geometry="AAAA",
        last_input_dir="C:/งาน",
    )
    user_settings.save(original, path)
    assert user_settings.load(path) == original
    assert "C:/งาน" in path.read_text(encoding="utf-8")


def test_corrupt_file_silently_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert user_settings.load(path) == UserSettings()
    path.write_text("[1, 2]", encoding="utf-8")
    assert user_settings.load(path) == UserSettings()
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert user_settings.load(path) == UserSettings()


def test_invalid_fields_fall_back_individually(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "stock_text": "8, 12",
                "stock_mode": "sideways",
                "kerf_enabled": "yes",
                "kerf_mm": 99,
                "time_limit_s": -5,
                "animations": False,
                "unknown_key": 1,
            }
        ),
        encoding="utf-8",
    )
    loaded = user_settings.load(path)
    defaults = UserSettings()
    assert loaded.stock_text == "8, 12"
    assert loaded.animations is False
    assert loaded.stock_mode == defaults.stock_mode
    assert loaded.kerf_enabled == defaults.kerf_enabled
    assert loaded.kerf_mm == defaults.kerf_mm
    assert loaded.time_limit_s == defaults.time_limit_s


def test_kerf_default_is_three_mm_unticked() -> None:
    defaults = UserSettings()
    assert (defaults.kerf_enabled, defaults.kerf_mm) == (False, 3)


def test_save_failure_is_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    user_settings.save(UserSettings(), blocker / "settings.json")  # parent is a file


def test_tests_never_use_the_real_appdata_file() -> None:
    assert user_settings.default_path().name == "user_settings.json"  # conftest isolation
