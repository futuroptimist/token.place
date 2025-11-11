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


def test_implement_prompt_offers_random_selection_walkthrough() -> None:
    """Prompt should spell out a concrete, reproducible selection workflow."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")

    assert "random selection checklist" in text.lower(), (
        "docs/prompts/codex/implement.md must label a random selection checklist to clarify the workflow"
    )
    assert "python - <<'py'" in text.lower(), (
        "docs/prompts/codex/implement.md must include an example python snippet for deterministic random selection"
    )


def test_implement_prompt_details_todo_cleanup_search() -> None:
    """Prompt should teach contributors how to confirm stale TODOs are gone."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert "after cleanup" in text_lower, (
        "docs/prompts/codex/implement.md must explicitly tie the TODO search to the post-cleanup step"
    )
    assert 'rg -F "TODO: refresh prompt-implement guide" -n' in text, (
        "docs/prompts/codex/implement.md must provide a concrete ripgrep command for verifying TODO removal"
    )


def test_implement_prompt_filters_out_generated_noise() -> None:
    """Prompt should remind contributors to exclude vendor and sample directories."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")

    assert "!**/node_modules/**" in text, (
        "docs/prompts/codex/implement.md must show how to ignore vendor directories when hunting TODO markers"
    )
