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
  one candidate at random and explain the selection. Record the command used for the
  random selection in your notes so reviewers can replay it.
- Keep changes narrowly scoped and prefer test-guided iterations.

PRE-FLIGHT CHECKLIST:
- Review repository instructions in [AGENTS.md](../../../AGENTS.md) and
  [docs/AGENTS.md](../../AGENTS.md).
- Skim [.github/workflows/](../../../.github/workflows/) so local runs mirror CI expectations.
- Read [README.md](../../../README.md), [DEVELOPMENT.md](../../DEVELOPMENT.md), and
  neighboring modules before editing security-sensitive paths.
- Use `rg` to enumerate TODO/FIXME/future-work markers across code, docs, and tests—pick
  one at random that still matters and has clear acceptance criteria.
- Install dependencies via `npm ci`, `pip install -r requirements.txt`,
  `pip install -r config/requirements_server.txt`, and
  `pip install -r config/requirements_relay.txt` before running checks.
- Run `playwright install` so browser binaries are ready for Playwright-powered tests.
- Plan to run `pre-commit run --all-files`, `npm run lint`, `npm run test:ci`, and
  `./run_all_tests.sh` when applicable.
- Scan staged changes for credentials with
  `detect-secrets scan $(git diff --cached --name-only)` (install via
  `pip install detect-secrets` if needed) prior to committing.

### Random selection checklist

1. Build the candidate list with
   ```bash
   rg --line-number "TODO|FIXME|future-work" \
      --glob '!**/node_modules/**' \
      --glob '!.git/**' \
      --glob '!hooks/**' \
      > /tmp/todo_list.txt
   ```

   Adjust the `--glob` filters if other vendor directories or sample fixtures introduce
   noise, then confirm the remaining paths still reflect real, actionable promises.
2. Trim the list to genuine promises: drop lines that only mention TODO tokens in
   tests/docs, weed out duplicates that describe the same work item, and note any
   removals so reviewers understand the filtering.
   - Ignore prompt text or other instructional references that only cite TODO/FIXME as
     examples—they are noise, not promises you can ship.
   - Skim 3–5 lines of surrounding context (for example, `sed -n '120,125p path/to/file'`)
     around each candidate so you understand the promised behavior before keeping it.
   - While pruning, identify what "done" means for each entry and jot down the
     smallest verifiable slice you could ship. This keeps future you honest about
     scope creep when you circle back to implement the fix.
   - Save the trimmed TODO list (for example, `/tmp/todo_filtered.txt`) and jot down
     why each entry was removed before running the randomizer so the narrowed pool is
     reproducible and auditable.
3. Confirm every surviving entry is still actionable (e.g., not already shipped or
   obsolete, scoped to a single verifiable improvement).
   - Write down a one-sentence acceptance criterion for the selected promise so the
     failing test you add later stays tightly scoped. Before moving on, record a
     one-line summary of the smallest verifiable slice you intend to ship now and explicitly defer
     the rest as follow-up TODOs. Translate that acceptance criterion into a failing test name or
     assertion before writing any code, and defer any extra assertions to follow-up TODOs to prevent
     scope creep. Keep a short non-goals list so reviewers understand what you're intentionally
     leaving for later. Include the non-goals list in the PR summary next to the smallest verifiable
     slice so reviewers can see what stays out of scope this round.
4. Use a deterministic randomizer so reviewers can replay the draw. For example:

   ```bash
   python - <<'PY'
   from pathlib import Path
   import random

   random.seed(20241024)
   tasks = Path("/tmp/todo_list.txt").read_text().splitlines()
   print(random.choice(tasks))
   PY
   ```

5. Record the exact command(s) you ran alongside the winning candidate in your notes or PR
   description.
6. Include the filtered TODO list (or fallback candidate list) and the random selection command
   in your PR summary so reviewers can replay the draw even after temporary files are cleaned up.
7. If the pool is empty, explicitly note that outcome. Treat each bullet under the
   "Upgrade instructions" request (or each Unreleased changelog bullet) as its own candidate,
   rerun the deterministic draw against that fallback list, and document both selections.
   - Explain why the primary pool was empty before switching lists, and keep the fallback draw separate
     from the trimmed TODO list instead of mixing the candidate sets.

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
   still delivers value now. Remember to record the command used for the random selection in your
   notes or PR description so others can reproduce the draw.
   - If the TODO/FIXME/future-work pool is empty, call that out in your notes and treat this
     prompt's "Upgrade instructions" block (or the Unreleased changelog entries) as the fallback
     source of promised work before proceeding. Treat each fallback bullet as an individual
     candidate, apply the same deterministic selection process, and keep both selection commands.
   - After the draw, freeze the scope: do not bundle opportunistic fixes or nearby TODOs into the
     same change. Instead, log follow-ups so the chosen slice remains small and auditable.
   - Add a succinct value statement to your PR summary explaining which user workflow or guardrail
     improves when the promise ships now. Call out affected tests or docs so reviewers can see the
     continued relevance at a glance.
   - When a TODO references multiple follow-ups, ship only the minimal slice that satisfies one
     verifiable promise and leave fresh TODOs for remaining scope. Call out the smallest verifiable
     slice in your notes so reviewers understand the intended boundary.
2. Add a failing automated test (pytest, Playwright, or equivalent scripted check) that captures
   the promised behavior, then make it pass with the minimal viable change.
3. Update docs, comments, and TODOs to reflect the shipped functionality; remove stale promises.
   - Replace or delete the original TODO marker instead of leaving it in a "done" state.
   - After cleanup, search for the original TODO text to confirm it is gone. For example, run:
     ```bash
     rg -F "TODO: refresh prompt-implement guide" -n
     ```
     and expect no matches before shipping.
   - Update any README or changelog references that mentioned the outstanding work.
4. Run the commands above and record their results in the PR description, noting any additional
   manual verification.
   - Verify each referenced command still runs and linked docs remain valid; call out any
     link updates in the PR description so reviewers can replay the workflow without surprises.
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
A pull request updating `docs/prompts/codex/implement.md` with passing checks and
documented impacts.
```
