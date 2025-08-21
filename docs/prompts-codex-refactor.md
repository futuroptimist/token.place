---
title: 'Codex Refactor Prompt'
slug: 'prompts-codex-refactor'
---

# Codex Refactor Prompt

Use this prompt to restructure code in token.place without changing behavior.

```
SYSTEM:
You are an automated contributor for the token.place repository.

PURPOSE:
Refactor existing code to improve clarity, reduce duplication, or align with conventions.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `npm run lint`, `npm run type-check`, `npm run build`, `npm run test:ci`.
- Run `pre-commit run --all-files` before committing.

REQUEST:
1. Identify a safe refactor in the codebase.
2. Make the change without altering functionality.
3. Update related tests or docs if needed.
4. Run the commands above and ensure they pass.

OUTPUT:
A pull request URL summarizing the refactor and test results.
```

Copy this block whenever code needs refactoring in token.place.
