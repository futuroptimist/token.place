"""Centralized color palette definitions for token.place brand assets."""

from __future__ import annotations

from typing import Dict

BRAND_COLORS: Dict[str, str] = {
    "primary_cyan": "#00FFFF",
    "accent_blue": "#007BFF",
    "accent_green": "#4CAF50",
    "background_dark": "#111111",
    "background_light": "#FFFFFF",
    "surface_dark": "#1A1A1A",
    "surface_light": "#F5F5F5",
    "text_on_dark": "#FFFFFF",
    "text_on_light": "#333333",
}

_PALETTES: Dict[str, Dict[str, str]] = {
    "dark": {
        "background": BRAND_COLORS["background_dark"],
        "surface": BRAND_COLORS["surface_dark"],
        "text": BRAND_COLORS["text_on_dark"],
        "accent": BRAND_COLORS["primary_cyan"],
        "supporting": BRAND_COLORS["accent_blue"],
    },
    "light": {
        "background": BRAND_COLORS["background_light"],
        "surface": BRAND_COLORS["surface_light"],
        "text": BRAND_COLORS["text_on_light"],
        "accent": BRAND_COLORS["accent_blue"],
        "supporting": BRAND_COLORS["accent_green"],
    },
}


def get_color_palette(mode: str) -> Dict[str, str]:
    """Return the named color palette for ``mode`` (``"dark"`` or ``"light"``).

    Args:
        mode: Display mode name.

    Returns:
        Mapping of semantic color roles to their hexadecimal values.

    Raises:
        ValueError: If ``mode`` is not a supported palette name.
    """

    key = mode.lower()
    if key not in _PALETTES:
        raise ValueError(f"Unsupported color palette '{mode}'")
    return _PALETTES[key].copy()
