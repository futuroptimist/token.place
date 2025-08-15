---
title: 'Codex Security Review Prompt'
slug: 'prompts-codex-security'
---

# Codex Security Review Prompt

Use this prompt to audit token.place for security issues and encryption integrity.

See also [Baseline Codex Prompt](prompts-codex.md) and
[Codex CI-Failure Fix Prompt](prompts-codex-ci-fix.md) for complementary guidance.

```
SYSTEM: Automated security reviewer for token.place.
GOAL: Harden crypto & dependency hygiene.
CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Do not log plaintext or ciphertext of user messages.
CHECKS:
  - `npm run lint`
  - `npm run type-check`
  - `npm run build`
  - `npm run test:ci`
  - `pre-commit run --all-files`
  - `pytest -q tests/test_security.py`
  - `bandit -r tokenplace -lll`
  - Verify README badges for Dependabot, CodeQL, secret-scanning.
FAIL if any badge missing or Bandit score \u2265 MEDIUM.
REQUEST:
1. Run all checks.
2. Inspect code for potential leaks or missing encryption steps.
3. Propose minimal patches that strengthen security if issues arise.
4. Re-run checks to confirm all pass.
5. Commit changes with a concise message and open a pull request.
OUTPUT_FORMAT: JSON
{"issues":[\u2026], "recommendations":[\u2026], "tests_pass":true}
If tests_pass is true, append the required patch in ```diff fences.
```

Copy this block whenever token.place needs a security review.
