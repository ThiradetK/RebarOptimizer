from __future__ import annotations

import math

import pytest

from rebar_optimizer.domain.errors import IssueCode, SettingsError
from rebar_optimizer.domain.models import StockSource
from rebar_optimizer.domain.settings import (
    OptimizerSettings,
    StockMode,
    default_num_workers,
    parse_stock_lengths_m,
)
from rebar_optimizer.i18n import th


def error_codes(settings: OptimizerSettings) -> list[IssueCode]:
    with pytest.raises(SettingsError) as exc:
        settings.validate()
    return [i.code for i in exc.value.issues]


def test_defaults_are_valid() -> None:
    settings = OptimizerSettings(stock_lengths_mm=(10000, 12000))
    settings.validate()
    assert settings.stock_mode is StockMode.MIXED
    assert settings.kerf_mm == 0
    assert settings.time_limit_s == 30
    assert settings.max_patterns == 50_000
    assert settings.random_seed == 0
    assert 1 <= settings.num_workers == default_num_workers() <= 8


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"stock_lengths_mm": ()}, [IssueCode.STOCK_EMPTY]),
        ({"stock_lengths_mm": (0,)}, [IssueCode.STOCK_NOT_POSITIVE]),
        ({"stock_lengths_mm": (-1000, 10000)}, [IssueCode.STOCK_NOT_POSITIVE]),
        ({"stock_lengths_mm": (10000, 10000)}, [IssueCode.STOCK_DUPLICATE]),
        ({"kerf_mm": -1}, [IssueCode.KERF_OUT_OF_RANGE]),
        ({"kerf_mm": 21}, [IssueCode.KERF_OUT_OF_RANGE]),
        ({"time_limit_s": 0}, [IssueCode.TIME_LIMIT_NOT_POSITIVE]),
        ({"time_limit_s": math.nan}, [IssueCode.TIME_LIMIT_NOT_POSITIVE]),
        ({"max_patterns": 0}, [IssueCode.MAX_PATTERNS_NOT_POSITIVE]),
        ({"num_workers": 0}, [IssueCode.NUM_WORKERS_NOT_POSITIVE]),
    ],
)
def test_each_invalid_setting(kwargs: dict[str, object], expected: list[IssueCode]) -> None:
    params: dict[str, object] = {"stock_lengths_mm": (10000,), **kwargs}
    assert error_codes(OptimizerSettings(**params)) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("kerf", [0, 3, 20])
def test_kerf_boundaries_valid(kerf: int) -> None:
    OptimizerSettings(stock_lengths_mm=(10000,), kerf_mm=kerf).validate()


def test_all_problems_reported_together() -> None:
    settings = OptimizerSettings(stock_lengths_mm=(), kerf_mm=50, time_limit_s=-1)
    assert error_codes(settings) == [
        IssueCode.STOCK_EMPTY,
        IssueCode.KERF_OUT_OF_RANGE,
        IssueCode.TIME_LIMIT_NOT_POSITIVE,
    ]


def test_settings_messages_are_thai() -> None:
    with pytest.raises(SettingsError) as exc:
        OptimizerSettings(stock_lengths_mm=(12000, 12000), kerf_mm=25).validate()
    messages = [th.format_issue(i) for i in exc.value.issues]
    assert messages == [
        "ความยาวเหล็กเส้น 12.000 ม. ซ้ำกัน",
        "ความกว้างใบตัดต้องอยู่ระหว่าง 0–20 มม. (พบ 25 มม.)",
    ]


def test_stock_options_sorted_new_unlimited() -> None:
    options = OptimizerSettings(stock_lengths_mm=(12000, 10000)).stock_options
    assert [o.length_mm for o in options] == [10000, 12000]
    assert all(o.available is None and o.source is StockSource.NEW for o in options)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10,12", (10000, 12000)),
        (" 10 , 12 ,", (10000, 12000)),
        ("12,10,12", (12000, 10000)),
        ("10.5", (10500,)),
        ("", ()),
        ("0,-3", (0, -3000)),
    ],
)
def test_parse_stock_lengths(text: str, expected: tuple[int, ...]) -> None:
    assert parse_stock_lengths_m(text) == expected


def test_parse_stock_lengths_reports_every_bad_item() -> None:
    with pytest.raises(SettingsError) as exc:
        parse_stock_lengths_m("10, abc, 12, inf")
    assert [i.params["text"] for i in exc.value.issues] == ["abc", "inf"]
    assert th.format_issue(exc.value.issues[0]) == 'ความยาวเหล็กเส้น "abc" ไม่ใช่ตัวเลข'
