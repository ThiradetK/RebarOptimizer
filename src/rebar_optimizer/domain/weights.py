"""Rebar unit weight."""

from __future__ import annotations

import re

_TRAILING_NUMBER = re.compile(r"(\d+(?:\.\d+)?)$")


def bar_diameter_mm(size: str) -> float | None:
    """Diameter from the trailing number of a size name: ``"DB12"`` -> 12."""
    match = _TRAILING_NUMBER.search(size.strip())
    if match is None:
        return None
    diameter = float(match.group(1))
    return diameter if diameter > 0 else None


def unit_weight_kg_per_m(size: str) -> float | None:
    """Nominal weight ``d²/162`` kg/m, or ``None`` if the diameter is unreadable."""
    diameter = bar_diameter_mm(size)
    if diameter is None:
        return None
    return diameter * diameter / 162
