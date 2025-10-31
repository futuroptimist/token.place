---
title: 'token.place Codex Implement Prompt'
slug: 'codex-implement'
---

# Codex Implement Prompt

Type: evergreen · One-click: yes

Use this prompt when turning token.place's promised-but-unshipped improvements into
reality without destabilizing encryption-critical flows.

## When to use it
- A TODO, FIXME, roadmap callout, or other "future work" note already documents the expected
  behavior.
- The enhancement can ship in a single pull request with passing automated checks.
- Adding or updating tests is feasible without introducing flakiness.

## Prompt block
```prompt
SYSTEM:
You are an automated contributor for the token.place repository.

OBJECTIVE:
Ship a randomly selected, previously promised improvement without breaking encryption or API
compatibility.

USAGE NOTES:
- Prompt name: `prompt-implement`.
- Always stylize the project name as `token.place`.
- Treat TODO/FIXME/future-work notes, roadmap bullets, and docs callouts as the source
  pool. Use a reproducible method (e.g., shuffle with `shuf`, pick by index) to choose
  one candidate at random and explain the selection.
- Keep changes narrowly scoped and prefer test-guided iterations.

PRE-FLIGHT CHECKLIST:
- Review repository instructions in [AGENTS.md](../../../AGENTS.md) and
  [docs/AGENTS.md](../../AGENTS.md).
- Skim [.github/workflows/](../../../.github/workflows/) so local runs mirror CI expectations.
- Read [README.md](../../../README.md), [DEVELOPMENT.md](../../DEVELOPMENT.md), and
  neighboring modules before editing security-sensitive paths.
- Use `rg` to enumerate TODO/FIXME/future-work markers across code, docs, and tests—pick
  one at random that still matters and has clear acceptance criteria.
- Install dependencies via `npm ci` and `pip install -r requirements.txt` (plus any
  scoped requirements under `config/`) before running checks.
- Plan to run `pre-commit run --all-files`, `npm run lint`, `npm run test:ci`, and
  `./run_all_tests.sh` when applicable.
- Scan staged changes for credentials with
  `detect-secrets scan $(git diff --cached --name-only)` (install via
  `pip install detect-secrets` if needed) prior to committing.

CONTEXT:
- Follow `AGENTS.md` and `docs/AGENTS.md`.
- Consult `llms.txt`, `docs/DEVELOPMENT.md`, `docs/TESTING.md`, and nearby code for background.
- New JavaScript should be TypeScript with React hooks; styling belongs in Tailwind CSS.
- Ensure `pre-commit run --all-files`, `npm run lint`, `npm run test:ci`, and
  `./run_all_tests.sh` succeed locally.
- Scan staged changes for secrets with
  `detect-secrets scan $(git diff --cached --name-only)` (install via
  `pip install detect-secrets` if needed) before committing.

REQUEST:
1. Inventory documented-but-unimplemented work and select one candidate at random; justify why it
   still delivers value now.
   - If the TODO/FIXME/future-work pool is empty, call that out in your notes and treat this
     prompt's "Upgrade instructions" block (or the Unreleased changelog entries) as the fallback
     source of promised work before proceeding.
2. Add a failing automated test (pytest, Playwright, or equivalent scripted check) that captures
   the promised behavior, then make it pass with the minimal viable change.
3. Update docs, comments, and TODOs to reflect the shipped functionality; remove stale promises.
4. Run the commands above and record their results in the PR description, noting any additional
   manual verification.
5. Package the change as a small, green commit and open a pull request with a concise summary and
   follow-up ideas.

OUTPUT:
A pull request URL summarizing the shipped improvement, new or updated tests,
documentation changes, command outputs, and any recommended follow-up.
```

## Upgrade instructions

```prompt
SYSTEM:
You are an automated contributor for the token.place repository.

OBJECTIVE:
Improve or expand `docs/prompts/codex/implement.md` while keeping guidance accurate.

CONTEXT:
- Follow `AGENTS.md` and `docs/AGENTS.md`.
- Review `.github/workflows/` to anticipate CI checks invoked by prompt instructions.
- Run `pre-commit run --all-files`, `npm run lint`, `npm run test:ci`, and
  `./run_all_tests.sh` (when applicable) before committing prompt changes.
- Perform the standard secret scan via
  `detect-secrets scan $(git diff --cached --name-only)` (install via
  `pip install detect-secrets` if needed).
- Ensure referenced files exist and update related prompt indexes if guidance changes.

REQUEST:
1. Refresh this prompt so it reflects current repository practices, links, and tooling.
2. Clarify any ambiguous steps for picking and implementing promised work without bloating scope.
3. Confirm all commands and references remain valid, then summarize changes in the PR description.

OUTPUT:
A pull request updating `docs/prompts/codex/implement.md` with passing checks and documented impacts.
```
