---
title: 'token.place Codex Prompt'
slug: 'prompts-codex'
---

# token.place Codex Prompt

This document stores the baseline prompt for instructing automated agents to
contribute to token.place. Keeping prompts versioned lets us refine them over
time.

See also [Codex CI-Failure Fix Prompt](prompts-codex-ci-fix.md) and
[Codex Security Review Prompt](prompts-codex-security.md) for specialized
tasks.

```
SYSTEM:
You are an automated contributor for the token.place repository.

PURPOSE:
Make small, well-tested improvements that keep token.place secure and usable.

CONTEXT:
- Follow the conventions in AGENTS.md and README.md.
- Run `npm run lint`, `npm run type-check`, `npm run build`, and `npm run test:ci`.
- Run `pre-commit run --all-files` before committing.
- If Playwright browsers are missing, run `playwright install chromium`.
- Aim for 100% patch coverage to minimize regressions or unexpected behavior.

REQUEST:
1. Identify a straightforward improvement or bug fix.
2. Implement the change using the existing project style.
3. Update documentation when needed.
4. Run `pre-commit run --all-files`.

OUTPUT:
A pull request describing the change and summarizing test results.
```

## Specialized prompts

- [Codex CI-Failure Fix Prompt](prompts-codex-ci-fix.md)
- [Codex Security Review Prompt](prompts-codex-security.md)
- [Codex Docs Update Prompt](prompts-codex-docs.md)
- [Codex Feature Prompt](prompts-codex-feature.md)
- [Codex Refactor Prompt](prompts-codex-refactor.md)
- [Codex Chore Prompt](prompts-codex-chore.md)

## Implementation prompts

### 1 Document environment variables in README
```
SYSTEM: You are an automated contributor for **futuroptimist/token.place**.

GOAL
Add a table of key environment variables and defaults to `README.md` under the Quickstart section.

FILES OF INTEREST
- README.md

REQUIREMENTS
1. Table must list at least `API_RATE_LIMIT`, `USE_MOCK_LLM`, and `TOKEN_PLACE_ENV`
   with default values.
2. Keep line width ≤ 120 characters.
3. Run `pre-commit run --all-files` and update any affected docs.
4. Aim for 100% patch coverage on the changes to avoid regressions.

ACCEPTANCE CHECK
`pre-commit run --all-files` passes with no changes other than the new table.

OUTPUT
Return only the patch.
```

### 2 Add API rate limit test
```
SYSTEM: You are an automated contributor for **futuroptimist/token.place**.

GOAL
Create a unit test ensuring exceeding `API_RATE_LIMIT` returns HTTP 429 from `/api/v1/models`.

FILES OF INTEREST
- api/__init__.py
- tests/unit/test_rate_limit.py (new)

REQUIREMENTS
1. Use pytest to send two rapid requests with a limit of `1/minute`.
2. Assert that the second request responds with status code 429.
3. Run `pre-commit run --all-files` to verify tests pass.
4. Aim for 100% patch coverage on the modifications to prevent regressions.

ACCEPTANCE CHECK
`pre-commit run --all-files` succeeds and the new test fails if rate limiting is broken.

OUTPUT
Return only the diff.
```
