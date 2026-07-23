from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "claude.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_claude_action_is_pinned_and_comment_context_is_trusted_only():
    assert "anthropics/claude-code-action@48698f76cceef03bcc3a54a17497fbfdd60d95fb" in TEXT
    assert "anthropics/claude-code-action@v1" not in TEXT
    assert 'include_comments_by_actor: "${{ github.repository_owner }},${{ vars.CLAUDE_TRUSTED_ACTORS }}"' in TEXT
    assert "author_association == 'OWNER'" not in TEXT
    assert "vars.CLAUDE_TRUSTED_ACTORS" in TEXT


def test_privileged_claude_steps_keep_sensitive_output_and_fallbacks_disabled():
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in TEXT
    assert "show_full_output: false" in TEXT
    assert "display_report: false" in TEXT
    assert "bypassPermissions" not in TEXT
    assert "Bash(node --check" not in TEXT
    assert "--dangerously-skip-permissions" not in TEXT


def test_exec_bash_gate_allows_only_fixed_validation_wrappers():
    allowed_line = next(line for line in TEXT.splitlines() if "--allowedTools" in line and "Bash(" in line)
    forbidden_fragments = [
        "Bash(npm",
        "Bash(npx",
        "Bash(node",
        "Bash(python",
        "Bash(curl",
        "Bash(wget",
        "Bash(gh",
        "Bash(git",
        "Bash(*)",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in allowed_line

    for wrapper in [
        "lint",
        "formatting-check",
        "typecheck",
        "unit-tests",
        "build",
        "repository-tests",
        "env-network-check",
    ]:
        assert f"/{wrapper})" in allowed_line
        assert f"exec \"$(dirname \"$0\")/_sandbox\" {wrapper}" in TEXT


def test_wrapper_rejects_arguments_and_compound_commands():
    assert "This fixed validation wrapper accepts no arguments." in TEXT
    assert "Only the fixed validation wrappers may run via Bash" in TEXT
    assert "compares the literal command string" in TEXT
    assert "tool_input.command // empty" in TEXT

if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print("claude workflow policy checks passed")
