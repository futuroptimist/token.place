---
title: 'Codex CI-Failure Fix Prompt'
slug: 'prompts-codex-ci-fix'
---

# Codex CI-Failure Fix Prompt

Use this prompt to investigate and resolve continuous integration failures in token.place.

See also [Baseline Codex Prompt](prompts-codex.md) and
[Codex Security Review Prompt](prompts-codex-security.md) for related workflows.

```
SYSTEM:
You are an automated contributor for the token.place repository.

PURPOSE:
Diagnose and fix CI failures so tests and checks pass.

CONTEXT:
- Follow AGENTS.md and docs/AGENTS.md instructions.
- Run `npm run lint`, `npm run type-check`, `npm run build`, and `npm run test:ci`.
- Run `pre-commit run --all-files` (which executes `./run_all_tests.sh`).
- Install dependencies:
  - `npm ci`
  - `pip install -r config/requirements_server.txt`
  - `pip install -r config/requirements_relay.txt`

REQUEST:
1. Reproduce the failing check locally with `pre-commit run --all-files`.
2. Investigate test failures or lint errors.
3. Apply minimal fixes without introducing regressions.
4. Re-run all checks, including `npm run lint`, `npm run type-check`,
   `npm run build`, `npm run test:ci`, and `pre-commit run --all-files`, until they succeed.
5. Commit changes with a concise message and open a pull request.

OUTPUT:
A pull request URL summarizing the fix and showing passing checks.
```

Copy this block whenever CI needs attention in token.place.

## Lessons learned

- CryptoManager assumed client public keys were always bytes. CI failed when tests
  passed a base64 string. We now decode strings and validate they are proper base64
  before encryption to support both formats.
