from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/claude.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_claude_action_is_pinned_and_filters_comment_context() -> None:
    assert "anthropics/claude-code-action@48698f76cceef03bcc3a54a17497fbfdd60d95fb" in TEXT
    assert "include_comments_by_actor" in TEXT
    assert "exclude_comments_by_actor" not in TEXT
    assert "vars.CLAUDE_TRUSTED_ACTORS" in TEXT
    assert "author_association == 'OWNER'" not in TEXT


def test_privileged_jobs_keep_secret_steps_after_authorization_and_checkout_guard() -> None:
    first_checkout = TEXT.index("- name: Checkout repository")
    first_secret = TEXT.index("claude_code_oauth_token")
    first_reject = TEXT.index("- name: Reject fork pull requests")
    assert first_reject < first_checkout < first_secret
    assert "persist-credentials: false" in TEXT
    assert "use_commit_signing: true" in TEXT
    assert "id-token: write" in TEXT


def test_claude_bash_permissions_are_fixed_validation_wrappers_only() -> None:
    forbidden = [
        "Bash(node --check *)",
        "Bash(npm",
        "Bash(npx",
        "Bash(node",
        "Bash(python",
        "Bash(curl",
        "Bash(wget",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "show_full_output: true",
    ]
    for needle in forbidden:
        assert needle not in TEXT

    for wrapper in [
        "python-dependency-compatibility",
        "python-tests",
        "frontend-lint",
        "typecheck",
        "build",
        "repository-tests",
        "env-network-check",
    ]:
        assert f"Bash(${{{{ steps.wrappers.outputs.dir }}}}/{wrapper})" in TEXT


def test_wrapper_rejects_arguments_and_uses_sandbox_with_cleared_env_and_no_network() -> None:
    assert "This fixed validation wrapper accepts no arguments." in TEXT
    assert "--clearenv" in TEXT
    assert "--unshare-net" in TEXT
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: \"1\"" in TEXT
    assert "Only the fixed validation wrappers may run via Bash" in TEXT


def test_system_prompt_disclaims_skipped_validation() -> None:
    assert "never present the Claude job itself as validation proof" in TEXT
    assert "report exactly what could not be performed" in TEXT


def test_authorization_covers_owner_configured_trusted_actor_and_rejects_forks() -> None:
    assert "github.event.comment.user.login == github.repository_owner" in TEXT
    assert "github.event.review.user.login == github.repository_owner" in TEXT
    assert "github.event.issue.user.login == github.repository_owner" in TEXT
    assert "vars.CLAUDE_TRUSTED_ACTORS || 'futuroptimist'" in TEXT
    assert "head.repo.full_name" in TEXT
    assert "Claude executable tools are disabled for fork PRs." in TEXT


def test_wrapper_guard_rejects_compound_commands_and_interpreter_flags_by_exact_match() -> None:
    assert "compares the literal command string" in TEXT
    assert "bash -c" in TEXT
    assert "pipelines/chains" in TEXT
    assert "appended" in TEXT
    assert "jq -r '.tool_input.command // empty'" in TEXT
