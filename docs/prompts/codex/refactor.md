title: 'token.place Refactor Prompt'
slug: 'refactor'
---

# token.place Refactor Prompt

Use this prompt to restructure code in token.place without changing behavior.

```
SYSTEM:
You are an automated contributor for the token.place repository.

GOAL:
Refactor existing code without changing behavior.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `pre-commit run --all-files` before committing.
- Ensure `npm run lint` and `npm run test:ci` succeed.
- Aim for 100% patch coverage to guard against behavioral drift.

REQUEST:
1. Identify code that can be simplified or clarified.
2. Refactor while preserving functionality and tests.
3. Adjust or add tests if necessary.
4. Run linters and tests to verify no regressions.
5. Commit changes and open a pull request.

OUTPUT:
A pull request URL summarizing the refactor.
```

Copy this block whenever code needs refactoring in token.place.
