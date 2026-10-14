from __future__ import annotations

import pytest

from rebar_optimizer.domain.weights import bar_diameter_mm, unit_weight_kg_per_m


@pytest.mark.parametrize(
    ("size", "diameter"),
    [("DB12", 12), ("RB9", 9), ("12", 12), ("DB25", 25), ("DB", None), ("X", None), ("DB0", None)],
)
def test_bar_diameter(size: str, diameter: float | None) -> None:
    assert bar_diameter_mm(size) == diameter


def test_db12_twelve_metres_weighs_about_10_67_kg() -> None:
    weight = unit_weight_kg_per_m("DB12")
    assert weight is not None
    assert weight * 12 == pytest.approx(10.67, abs=0.005)


def test_unreadable_size_has_no_weight() -> None:
    assert unit_weight_kg_per_m("ปลอก") is None
