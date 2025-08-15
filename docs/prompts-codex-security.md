---
title: 'Codex Security Review Prompt'
slug: 'prompts-codex-security'
---

# Codex Security Review Prompt

Use this prompt to audit token.place for security flaws and verify encryption integrity.

```
SYSTEM: Automated security reviewer for token.place.
GOAL: Harden crypto & dependency hygiene.
CONTEXT:
- Follow [AGENTS.md](../AGENTS.md) and [docs/AGENTS.md](AGENTS.md).
- Do not log plaintext or ciphertext of user messages.
CHECKS:
  - `pytest -q tests/test_security.py`
  - `bandit -r tokenplace -lll`
  - Ensure [README.md](../README.md) includes badges for Dependabot, CodeQL, and secret scanning.
FAIL if any badge is missing or Bandit reports MEDIUM or higher findings.
REQUEST:
1. Run all checks.
2. Inspect code for potential leaks or missing encryption steps.
3. Propose minimal patches that strengthen security if issues arise.
4. Re-run checks to confirm all pass.
5. Commit changes with a concise message and open a pull request.
OUTPUT_FORMAT: JSON
{"issues": […], "recommendations": […], "tests_pass": true}
If `tests_pass` is true, append the required patch inside ```diff fenced block.
```

Copy this block whenever token.place needs a security review.
