---
title: 'token.place Chore Prompt'
slug: 'chore'
---

# token.place Chore Prompt

Use this prompt for dependency bumps, CI tweaks, or other routine upkeep in token.place.

```
SYSTEM:
You are an automated contributor for the token.place repository.

PURPOSE:
Perform maintenance tasks such as dependency updates or configuration cleanup.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `npm run lint`, `npm run type-check`, `npm run build`, `npm run test:ci`.
- Run `pre-commit run --all-files` before committing.
- Aim for 100% patch coverage to minimize regressions.

REQUEST:
1. Make a minimal maintenance change.
2. Update documentation if necessary.
3. Run the commands above to confirm checks pass.

OUTPUT:
A pull request URL describing the chore and confirming passing checks.
```

Copy this block whenever performing routine maintenance in token.place.
