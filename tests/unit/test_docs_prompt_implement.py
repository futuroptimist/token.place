"""Regression test for docs/prompts/codex/implement.md guidance."""
from __future__ import annotations

import pathlib


def test_implement_prompt_explains_empty_todo_fallback() -> None:
    """Prompt should spell out what to do when no TODO markers are found."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")

    assert "TODO/FIXME" in text, "Sanity check: prompt should mention TODO markers."
    assert "pool is empty" in text or "no TODO" in text.lower(), (
        "docs/prompts/codex/implement.md must describe the fallback when the TODO pool is empty"
    )


def test_implement_prompt_requires_logging_random_method() -> None:
    """Prompt should demand documenting how the random candidate was picked."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")

    needle = "record the command used for the random selection"
    assert needle in text.lower(), (
        "docs/prompts/codex/implement.md must tell contributors to record the command used for the random selection"
    )
