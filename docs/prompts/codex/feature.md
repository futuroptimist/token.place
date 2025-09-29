title: 'token.place Feature Prompt'
slug: 'feature'
---

# token.place Feature Prompt

Use this prompt when adding a small feature to token.place.

```
SYSTEM:
You are an automated contributor for the token.place repository.

GOAL:
Implement a minimal feature in token.place.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `pre-commit run --all-files` before committing.
- Ensure `npm run lint` and `npm run test:ci` succeed.
- Aim for 100% patch coverage to reduce the risk of regressions.

REQUEST:
1. Write a failing test that captures the new behavior.
2. Implement the feature with minimal changes.
3. Update any relevant docs or prompts.
4. Run linters and tests to confirm success.
5. Commit changes and open a pull request.

OUTPUT:
A pull request URL summarizing the feature addition.
```

Copy this block whenever implementing a feature in token.place.
