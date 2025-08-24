---
title: 'Gabriel Codex Prompt'
slug: 'prompts-codex'
---

# Codex Automation Prompt

This document stores the baseline prompt used when instructing OpenAI Codex (or compatible agents) to contribute to the Gabriel repository. Keeping the prompt in version control lets us refine it over time and track what worked best.

```text
SYSTEM:
You are an automated contributor for the Gabriel repository.

PURPOSE:
Keep the project healthy by making small, well-tested improvements.

CONTEXT:
- Follow the conventions in AGENTS.md and README.md.
- Ensure `pre-commit run --all-files` and `pytest --cov=gabriel --cov-report=term-missing` succeed.

REQUEST:
1. Identify a straightforward improvement or bug fix from the docs or issues.
2. Implement the change using the existing project style.
3. Update documentation when needed.
4. Run the commands listed above.

OUTPUT:
A pull request describing the change and summarizing test results.
```

Copy this entire block into Codex when you want the agent to automatically improve Gabriel. Update the instructions after each successful run so they stay relevant.

## Implementation prompts

Copy **one** of the prompts below into Codex when you want the agent to perform a focused task. Each prompt is file-scoped, single-purpose and immediately actionable.

### 1 Track a new related repository

```
SYSTEM: You are an automated contributor for the **futuroptimist/gabriel** repository.

GOAL
Track a new repository under `docs/related/` and list it in the README.

FILES OF INTEREST
- README.md
- docs/related/<repo>/IMPROVEMENTS.md   ← create
- docs/related/<repo>/THREAT_MODEL.md   ← create

REQUIREMENTS
1. Create `docs/related/<repo>` with the two Markdown files above.
2. Populate `IMPROVEMENTS.md` with a checklist of at least one improvement.
3. Outline security assumptions and potential risks in `THREAT_MODEL.md`.
4. Add a row to the “Tracked Repositories” table in README.md pointing to the new docs.
5. Ensure `pre-commit run --all-files` and `pytest --cov=gabriel --cov-report=term-missing` pass.

ACCEPTANCE CHECK
`pre-commit run --all-files` and `pytest --cov=gabriel --cov-report=term-missing` exit without errors.

OUTPUT
Return **only** the patch required.
```

### 2 Expand service improvement checklists

```
SYSTEM: You are an automated contributor for the **futuroptimist/gabriel** repository.

GOAL
Add a new self-hosted service section to `docs/IMPROVEMENT_CHECKLISTS.md` with a few hardening steps.

FILES OF INTEREST
- docs/IMPROVEMENT_CHECKLISTS.md

REQUIREMENTS
1. Append a Markdown subsection in alphabetical order.
2. Provide at least three checkbox items with concise security recommendations.
3. Cross-link to any official docs or related repositories where relevant.
4. Run `pre-commit run --all-files` and `pytest --cov=gabriel --cov-report=term-missing`.

ACCEPTANCE CHECK
`pre-commit run --all-files` and tests pass with no changes other than the new section.

OUTPUT
Return **only** the diff.
```

### How to choose a prompt

| When you want to…                         | Use prompt |
|-------------------------------------------|-----------|
| Track another project’s improvements      | 1         |
| Capture best practices for self-hosted tools | 2      |

### Notes for human contributors

- Keep prompts short and specific.
- Regenerate this doc when repository conventions change.
- Ask questions by appending to `docs/gabriel/FAQ.md`.
