from pathlib import Path
import unittest

TEXT = Path('.github/workflows/claude.yml').read_text()


class ClaudeWorkflowPolicyTest(unittest.TestCase):
    def test_claude_action_is_sha_pinned_and_filters_comment_context(self) -> None:
        self.assertIn('anthropics/claude-code-action@48698f76cceef03bcc3a54a17497fbfdd60d95fb # v1', TEXT)
        self.assertIn("include_comments_by_actor: ${{ format('{0},futuroptimist,{1}', github.event.repository.owner.login, vars.CLAUDE_TRUSTED_ACTORS) }}", TEXT)
        self.assertNotIn('show_full_output: true', TEXT)
        self.assertNotIn('bypassPermissions', TEXT)

    def test_authorization_uses_owner_or_trusted_actors_not_association(self) -> None:
        self.assertNotIn('author_association', TEXT)
        self.assertIn('github.event.repository.owner.login', TEXT)
        self.assertIn('vars.CLAUDE_TRUSTED_ACTORS', TEXT)
        self.assertIn('futuroptimist', TEXT)

    def test_privileged_steps_remain_after_trigger_and_fork_gates(self) -> None:
        first_checkout = TEXT.index('uses: actions/checkout@v4')
        first_fork_reject = TEXT.index('Reject fork pull requests')
        first_secret = TEXT.index('claude_code_oauth_token:')
        self.assertLess(first_fork_reject, first_checkout)
        self.assertLess(first_checkout, first_secret)
        self.assertIn('persist-credentials: false', TEXT)
        self.assertIn('use_commit_signing: true', TEXT)

    def test_wrapper_allowlist_excludes_generic_shell_and_interpreters(self) -> None:
        allowed_lines = [line for line in TEXT.splitlines() if '--allowedTools' in line]
        joined = '\n'.join(allowed_lines)
        for pattern in ['Bash(npm', 'Bash(npx', 'Bash(node', 'Bash(python', 'Bash(curl', 'Bash(wget', 'Bash(gh', 'Bash(git']:
            self.assertNotIn(pattern, joined)
        for wrapper in ['frontend-lint', 'typecheck', 'build', 'build-client', 'format-check', 'repository-tests']:
            self.assertIn(f'${{{{ steps.wrappers.outputs.dir }}}}/{wrapper}', joined)

    def test_sandbox_fails_closed_and_scrubs_environment(self) -> None:
        self.assertIn('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"', TEXT)
        self.assertIn('--unshare-net', TEXT)
        self.assertIn('--clearenv', TEXT)
        self.assertIn('refusing to fall back to unsandboxed validation commands', TEXT)
        self.assertIn('secret-bearing environment variables visible', TEXT)

    def test_guard_rejects_arguments_and_compound_commands(self) -> None:
        self.assertIn('This fixed validation wrapper accepts no arguments.', TEXT)
        self.assertIn('compares the literal command string', TEXT)
        self.assertIn('Only the fixed validation wrappers may run via Bash', TEXT)
        self.assertIn('realpath -m -- "${candidate}"', TEXT)
        self.assertIn('resolves outside the repository workspace', TEXT)


if __name__ == '__main__':
    unittest.main()
