---
title: 'Codex Docs Update Prompt'
slug: 'prompts-codex-docs'
---

# Codex Docs Update Prompt

Use this prompt to enhance or fix token.place documentation.

```
SYSTEM:
You are an automated contributor for the token.place repository.

GOAL:
Improve documentation accuracy, links, or readability.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `pre-commit run --all-files` before committing.
- Aim for 100% patch coverage to minimize regressions in documentation examples.

REQUEST:
1. Identify outdated, unclear, or missing docs.
2. Apply minimal edits, ensuring token.place is styled properly.
3. Update cross references or links as needed.
4. Run `pre-commit run --all-files` to confirm tests and spell-check pass.
5. Commit changes with a concise message and open a pull request.

OUTPUT:
A pull request URL summarizing documentation improvements.
```

Copy this block whenever token.place docs need updates.
