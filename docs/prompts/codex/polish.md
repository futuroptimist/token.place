---
title: 'token.place Polish Prompt'
slug: 'polish'
---

Copy the desired prompt into Codex and follow it verbatim.

## Prompt
```prompt
SYSTEM:
You are an automated contributor for the token.place repository.

OBJECTIVE:
Deliver structural polish that preserves current behavior while improving clarity,
maintainability, and developer experience.

SNAPSHOT (AUTO-DETECTED):
- Languages: Python (`api/`, `server/`, `utils/`, `encrypt.py`, `relay.py`), TypeScript/JavaScript
  (`desktop/`, `static/`, `tests/e2e/`), Shell (`scripts/`, `run_all_tests.sh`), PowerShell
  (`run_all_tests.ps1`).
- Major directories: `api/`, `server/`, `desktop/`, `k8s/`, `docker/`, `tests/`, plus
  `config/`, `scripts/`, `static/`, `utils/`.
- CI & security gates: `.github/workflows/ci.yml` runs `run_all_tests.sh` (pytest + Playwright + npm);
  `codeql.yml` enforces CodeQL; `desktop*.yml` package Electron builds; `docker.yml` builds images;
  Bandit enforced via `tests/test_security_bandit.py`; coverage uploaded through Codecov.
- Tooling present: `Makefile`, root `docker-compose.yml`, `requirements.txt` plus
  `config/requirements_{server,relay}.txt`, `package.json`, Playwright suites in
  `tests/test_crypto_compatibility_playwright.py` & `tests/e2e/`.

HIGH-ROI REFACTOR PLAN:
1. Layout unification — consolidate Python entrypoints under `apps/` (`apps/server/`, `apps/relay/`)
   with shared `apps/common/` for config, crypto, and transport helpers; move web & Electron assets
   beneath `clients/` (`clients/web/`, `clients/desktop/`) with shared `clients/common/`; collect
   infra into `infra/docker/` + `infra/k8s/`; keep docs in `docs/` with a prompt index.
2. Config hardening — create a single typed loader (`apps/common/config.py`) consuming
   `config/defaults.yaml`, `.env`, `.env.local`, and CLI/env overrides with documented precedence;
   add `config/*.example` templates and migrate ad-hoc `os.environ` reads to the module.
3. Moderation & fallback — encapsulate rate limiting, moderation, and failover logic behind
   interfaces (e.g., `apps/common/policies.py`); write contract tests covering `API_FALLBACK_URLS`,
   Cloudflare tunnel fallback, and relay retries; record failure-handling matrices in
   `docs/INCIDENT_PLAYBOOK.md`.
4. Clients — publish a shared SDK (crypto helpers, auth, telemetry) reused by Python CLI, Electron
   desktop, and browser bundles; normalize retry/timeouts headers; extend Playwright E2E coverage for
   Python ↔ relay, browser ↔ relay, desktop ↔ relay flows; document workspace/package sharing.
5. Observability — adopt structured logging (`structlog` or enriched `logging`), propagate request IDs
   across services, emit minimal anonymized metrics (OTLP/exporters) with opt-in toggles.

DIRECTORY HYGIENE:
- Reduce root clutter by relocating operational scripts and compose files under `infra/` and
  customer-facing assets under `clients/`.
- Ensure README and docs contain a "Map of the repo" leading to setup → run → test → deploy within
  three clicks; keep prompt docs indexed from `docs/README.md`.

QUALITY & SAFETY:
- ✅ Drafted a security review checklist for relay failovers, Cloudflare fallback, and key
  management; documented secrets boundaries, logging redaction, and audit steps in
  [docs/SECURITY_REVIEW_CHECKLIST.md](../../SECURITY_REVIEW_CHECKLIST.md).
- Add typed config validation, consistent error handling, and guardrails for environment overrides.

TESTING TARGETS:
- Unit tests for moderation hooks, config loaders, and observability adapters.
- Contract tests verifying client ↔ relay ↔ provider protocols (Python SDK, web, desktop).
- Smoke tests for `docker-compose` profiles and Kubernetes overlays, plus link/spell check jobs in CI.

DOCS & DX:
- Extend README with quickstart matrices (dev/prod), environment variable tables, incident playbook
  links, and make targets for common flows; ensure `make` wraps setup/test/deploy commands.
- Keep `docs/prompts/codex/` synchronized via `scripts/migrate-prompt-docs.sh`; include docs preview
  checks (link, spell) in CI and local workflows.

ORTHOGONALITY TRACKER:
- Maintain a rubric comparing saturation of `implement.md` tasks vs. polish ROI; when variants collide
  or scope overlaps, prioritize this polish track and record decisions in `docs/roadmap.md`.

EXECUTION STEPS:
1. Inventory current layout/config/test gaps against the plan and open tracking issues.
2. Stage non-breaking moves (directory reshuffles, typed config modules) with thorough imports
   updated and migration scripts provided.
3. Update documentation (README map, incident playbook, config references) and regenerate prompt
   indexes.
4. Run `pre-commit run --all-files`, `npm run lint`, `npm run test:ci`, and `./run_all_tests.sh`; note
   link/spell checker results and run `python scripts/check_doc_links.py` if needed.
5. Scan staged changes for secrets via
   `detect-secrets scan $(git diff --cached --name-only)` (install via `pip install detect-secrets`).
6. Summarize outcomes, remaining debt, and orthogonality status in the PR description; merge when CI
   is green.

OUTPUT:
A single pull request that applies the curated polish without regressions, reports executed checks,
updates docs, and logs next steps in the orthogonality tracker.
```

## Upgrade Prompt
```prompt
SYSTEM:
You are an automated contributor for the token.place repository.

OBJECTIVE:
Improve `docs/prompts/codex/polish.md` so it stays accurate, concise, and aligned with repository
standards.

CONTEXT:
- Follow `AGENTS.md` and `docs/AGENTS.md`.
- Inspect `.github/workflows/` to mirror CI expectations in prompt guidance.
- Ensure references to directories, scripts, and tooling remain correct and reproducible.
- Maintain the fully fenced codeblock style with minimal human-facing text outside the prompt.
- Run `pre-commit run --all-files`, `npm run lint`, `npm run test:ci`, and documentation link/spell
  checks before committing prompt edits.
- Scan staged files using `detect-secrets scan $(git diff --cached --name-only)`.

REQUEST:
1. Refresh quick facts, refactor targets, and testing guidance to match the latest repository state.
2. Tighten language for clarity while preserving actionable, non-breaking polish steps.
3. Verify linked files exist and update prompt indexes or docs references when paths change.
4. Record executed checks in the PR body and call out follow-up opportunities for future polish.

OUTPUT:
A merged pull request updating `docs/prompts/codex/polish.md`, documenting executed checks, and noting
any recommended follow-up work.
```
