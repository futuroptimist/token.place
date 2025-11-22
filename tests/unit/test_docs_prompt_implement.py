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


def test_implement_prompt_reinforces_minimal_scope() -> None:
    """Prompt should clarify how to keep the work scoped to a single verifiable slice."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")

    assert "smallest verifiable slice" in text.lower(), (
        "docs/prompts/codex/implement.md must explicitly tell contributors to ship the smallest verifiable slice"
    )


def test_implement_prompt_ignores_instructional_noise() -> None:
    """Prompt should avoid treating instructional TODO mentions as actionable work and set non-goals."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text_lower = prompt_path.read_text(encoding="utf-8").lower()

    assert "instructional references" in text_lower, (
        "docs/prompts/codex/implement.md must warn against counting instructional TODO mentions as candidates"
    )
    assert "non-goal" in text_lower, (
        "docs/prompts/codex/implement.md must ask contributors to note non-goals to prevent scope creep"
    )


def test_implement_prompt_preserves_trimmed_todo_list() -> None:
    """Prompt should require saving the filtered TODO list and its rationale."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text_lower = prompt_path.read_text(encoding="utf-8").lower()

    assert "trimmed todo list" in text_lower, (
        "docs/prompts/codex/implement.md must tell contributors to keep the trimmed TODO list for the random draw"
    )
    assert "why each entry was removed" in text_lower, (
        "docs/prompts/codex/implement.md must remind contributors to note why entries were filtered out"
    )


def test_implement_prompt_requires_context_check() -> None:
    """Prompt should tell contributors to read nearby context before locking in a TODO."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text_lower = prompt_path.read_text(encoding="utf-8").lower()

    assert "surrounding context" in text_lower, (
        "docs/prompts/codex/implement.md must tell contributors to skim the surrounding context for each candidate"
    )
    assert "sed -n" in text_lower, (
        "docs/prompts/codex/implement.md must provide a concrete command for reading that nearby context"
    )


def test_implement_prompt_shares_randomization_artifacts() -> None:
    """Prompt should ask contributors to share reproducibility artifacts with reviewers."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text_lower = prompt_path.read_text(encoding="utf-8").lower()

    assert "filtered todo list" in text_lower and "pr summary" in text_lower, (
        "docs/prompts/codex/implement.md must direct contributors to surface the filtered TODO list in the PR summary"
    )
    assert "random selection command" in text_lower, (
        "docs/prompts/codex/implement.md must tell contributors to share the random selection command output"
    )


def test_implement_prompt_calls_out_config_requirements() -> None:
    """Prompt should reference the scoped Python requirement files explicitly."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")

    for requirement in ("config/requirements_server.txt", "config/requirements_relay.txt"):
        assert requirement in text, (
            "docs/prompts/codex/implement.md must remind contributors to install scoped requirements"
        )


def test_implement_prompt_demands_value_statement() -> None:
    """Prompt should insist on documenting why the chosen task still matters and capture acceptance criteria."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text = prompt_path.read_text(encoding="utf-8")
    text_lower = text.lower()

    assert "value statement" in text_lower, (
        "docs/prompts/codex/implement.md must tell contributors to include a value statement for the chosen task"
    )
    assert "pr summary" in text_lower, (
        "docs/prompts/codex/implement.md must mention the PR summary as the place to capture that value statement"
    )
    assert "one-sentence acceptance criterion" in text_lower, (
        "docs/prompts/codex/implement.md must instruct contributors to note the acceptance criteria before coding"
    )


def test_implement_prompt_ties_acceptance_to_failing_test() -> None:
    """Prompt should connect the acceptance criterion to a failing test and scoping guardrails."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text_lower = prompt_path.read_text(encoding="utf-8").lower()

    assert "translate that acceptance criterion into a failing test" in text_lower, (
        "docs/prompts/codex/implement.md must tell contributors to codify the acceptance criterion as a failing test"
    )
    assert "defer any extra assertions to follow-up todos" in text_lower, (
        "docs/prompts/codex/implement.md must remind contributors to park extra scope as follow-ups"
    )


def test_implement_prompt_confirms_commands_and_links() -> None:
    """Prompt should require checking referenced commands and links still work."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "docs" / "prompts" / "codex" / "implement.md"
    text_lower = prompt_path.read_text(encoding="utf-8").lower()

    assert "verify each referenced command still runs" in text_lower, (
        "docs/prompts/codex/implement.md must ask contributors to validate the commands it cites"
    )
    assert "link updates in the pr description" in text_lower, (
        "docs/prompts/codex/implement.md must remind contributors to note link changes for reviewers"
    )
