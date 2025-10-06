import re
from pathlib import Path

import pytest

from utils.branding.colors import BRAND_COLORS, get_color_palette


def test_brand_colors_cover_core_tokens():
    expected_keys = {
        "primary_cyan",
        "accent_blue",
        "accent_green",
        "background_dark",
        "background_light",
        "surface_dark",
        "surface_light",
        "text_on_dark",
        "text_on_light",
    }
    assert expected_keys.issubset(BRAND_COLORS.keys())

    hex_color = re.compile(r"^#[0-9A-F]{6}$")
    for name, value in BRAND_COLORS.items():
        assert hex_color.match(value), f"{name} must be a 6-digit uppercase hex color"

    dark_palette = get_color_palette("dark")
    assert dark_palette["background"] == BRAND_COLORS["background_dark"]
    assert dark_palette["surface"] == BRAND_COLORS["surface_dark"]
    assert dark_palette["text"] == BRAND_COLORS["text_on_dark"]
    assert dark_palette["accent"] == BRAND_COLORS["primary_cyan"]

    light_palette = get_color_palette("light")
    assert light_palette["background"] == BRAND_COLORS["background_light"]
    assert light_palette["surface"] == BRAND_COLORS["surface_light"]
    assert light_palette["text"] == BRAND_COLORS["text_on_light"]
    assert light_palette["accent"] == BRAND_COLORS["accent_blue"]

    with pytest.raises(ValueError):
        get_color_palette("sepia")


def test_style_guide_mentions_brand_palette():
    style_text = Path("docs/STYLE_GUIDE.md").read_text(encoding="utf-8")
    for name, value in BRAND_COLORS.items():
        token_label = name.replace("_", " ")
        assert token_label in style_text
        assert value in style_text
