"""Regression tests for documentation promises in docs/STYLE_GUIDE.md."""
from pathlib import Path

import pytest


def test_style_guide_includes_logo_usage_guidelines():
    """The style guide should document how to use the official logo assets."""
    guide_path = Path('docs/STYLE_GUIDE.md')
    guide_text = guide_path.read_text(encoding='utf-8')

    assert '[TBD: Add logo usage guidelines when a logo is created]' not in guide_text
    assert 'Logo usage guidelines' in guide_text
    assert 'static/favicon.svg' in guide_text
    assert 'static/icon.ico' in guide_text
    # Encourage contributors to maintain consistent safe-area guidance.
    assert 'clear space' in guide_text.lower()
