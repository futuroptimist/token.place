"""Regression tests for README streaming documentation."""
from pathlib import Path


def test_readme_streaming_section_exists():
    """README should document how to use streaming responses."""
    readme_path = Path("README.md")
    readme_text = readme_path.read_text(encoding="utf-8").lower()

    assert "## streaming usage" in readme_text, "README is missing the streaming usage section"
    assert (
        "\"stream\": true" in readme_text or "`stream`" in readme_text
    ), "README should mention enabling streaming via the `stream` flag"
    assert (
        "server-sent events" in readme_text or "sse" in readme_text
    ), "README should reference Server-Sent Events handling for streaming"
