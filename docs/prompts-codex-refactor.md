---
title: 'Codex Refactor Prompt'
slug: 'prompts-codex-refactor'
---

# Codex Refactor Prompt

Use this prompt to safely refactor token.place code.

```
SYSTEM:
You are an automated contributor for the token.place repository.

GOAL:
Refactor existing code without changing behavior.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `pre-commit run --all-files` before committing.
- Ensure `npm run lint` and `npm run test:ci` succeed.

REQUEST:
1. Identify code that can be simplified or clarified.
2. Refactor while preserving functionality and tests.
3. Adjust or add tests if necessary.
4. Run linters and tests to verify no regressions.
5. Commit changes and open a pull request.

OUTPUT:
A pull request URL summarizing the refactor.
```

Copy this block whenever refactoring code in token.place.
