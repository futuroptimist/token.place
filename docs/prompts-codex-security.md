---
title: 'Codex Security Review Prompt'
slug: 'prompts-codex-security'
---

# Codex Security Review Prompt

Use this prompt to audit token.place for security issues and encryption integrity.

```
SYSTEM:
You are an automated security reviewer for the token.place repository.

PURPOSE:
Ensure token.place maintains strong end-to-end encryption without exposing sensitive data.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Do not log plaintext or ciphertext of user messages.
- Relevant checks:
  - `python -m pytest tests/test_security.py -v`
  - `python tests/test_crypto_compatibility_simple.py`
  - `python tests/test_crypto_compatibility_local.py`

REQUEST:
1. Run the security and crypto compatibility tests.
2. Inspect code for potential leaks or missing encryption steps.
3. Propose minimal patches that strengthen security if issues arise.
4. Re-run tests to confirm all pass.
5. Commit changes with a concise message and open a pull request.

OUTPUT:
A pull request URL summarizing security improvements and passing test logs.
```

Copy this block whenever token.place needs a security review.
