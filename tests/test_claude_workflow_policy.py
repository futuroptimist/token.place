from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "claude.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_claude_action_is_pinned_and_filters_trusted_comment_context() -> None:
    assert "anthropics/claude-code-action@48698f76cceef03bcc3a54a17497fbfdd60d95fb" in TEXT
    assert "include_comments_by_actor: ${{ format('{0},{1}', github.repository_owner, vars.CLAUDE_TRUSTED_ACTORS || '') }}" in TEXT
    assert "github.event.comment.author_association == 'OWNER'" not in TEXT
    assert "github.event.review.author_association == 'OWNER'" not in TEXT
    assert "github.event.issue.author_association == 'OWNER'" not in TEXT


def test_privileged_jobs_use_owner_or_trusted_actor_allowlist_before_checkout() -> None:
    assert "github.event.comment.user.login == github.repository_owner" in TEXT
    assert "vars.CLAUDE_TRUSTED_ACTORS" in TEXT
    assert TEXT.index("github.event.comment.user.login == github.repository_owner") < TEXT.index("Checkout repository")
    assert TEXT.index("vars.CLAUDE_TRUSTED_ACTORS") < TEXT.index("Checkout repository")
    assert "persist-credentials: false" in TEXT


def test_claude_validation_surface_is_fixed_wrappers_not_generic_interpreters() -> None:
    allowed_line = next(line for line in TEXT.splitlines() if "--allowedTools" in line and "python-dependency-compatibility" in line)
    for operation in ["unit-tests", "lint", "formatting-check", "typecheck", "build", "repository-tests"]:
        assert f"Bash(${{{{ steps.wrappers.outputs.dir }}}}/{operation})" in allowed_line
    for forbidden in ["Bash(npm", "Bash(npx", "Bash(node", "Bash(python", "Bash(curl", "Bash(wget", "Bash(gh", "Bash(git "]:
        assert forbidden not in allowed_line
    assert "Bash(node --check" not in TEXT


def test_sandbox_scrubs_environment_and_disables_network_without_unsandboxed_fallback() -> None:
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: \"1\"" in TEXT
    assert "--unshare-net" in TEXT
    assert "--clearenv" in TEXT
    assert "refusing to fall back to unsandboxed validation commands" in TEXT
    assert "--dangerously-skip-permissions" not in TEXT
    assert "bypassPermissions" not in TEXT
    assert "show_full_output: true" not in TEXT
