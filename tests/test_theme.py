from __future__ import annotations

import re
from pathlib import Path

import pytest

from rebar_optimizer.ui.theme import DARK, LIGHT, Theme, stylesheet

pytestmark = pytest.mark.gui


@pytest.mark.parametrize("theme", [LIGHT, DARK])
def test_indicators_are_drawn_explicitly(theme: Theme) -> None:
    """Fusion's own radio/check outline vanishes on a dark window."""
    qss = stylesheet(theme)
    assert "QRadioButton::indicator" in qss
    assert f"border: 1px solid {theme.indicator}" in qss
    icon = re.search(r'image: url\("([^"]+)"\)', qss)
    assert icon is not None and Path(icon.group(1)).is_file()
    assert "\\" not in icon.group(1)  # QSS url() wants forward slashes


def test_indicator_outline_contrasts_with_surface() -> None:
    def luminance(color: str) -> float:
        r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    for theme in (LIGHT, DARK):
        assert abs(luminance(theme.indicator) - luminance(theme.surface)) > 0.3
