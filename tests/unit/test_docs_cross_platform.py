"""Regression tests for docs/CROSS_PLATFORM.md promises."""
from pathlib import Path

import pytest


@pytest.mark.unit
def test_cross_platform_docs_acknowledge_streaming_tests():
    """Cross-platform guide should note that streaming tests are available."""
    guide_path = Path('docs/CROSS_PLATFORM.md')
    guide_text = guide_path.read_text(encoding='utf-8')

    assert 'Streaming tests are skipped as that feature is still in development' not in guide_text
    assert 'tests/test_streaming.py' in guide_text
    assert 'tests/test_e2e_conversation_flow.py' in guide_text
    assert 'streaming tests now run across supported platforms' in guide_text.lower()
