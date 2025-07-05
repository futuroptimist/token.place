# 🤖 AGENTS

This repo follows the lightweight assistant workflow from [flywheel](https://github.com/futuroptimist/flywheel).
Use these helpers to keep `token.place` healthy.

See [docs/AGENTS.md](docs/AGENTS.md) for the full contributor guide and [llms.txt](llms.txt) for a quick machine-readable summary.

## Code Linter Agent
- **When:** every pull request
- **Does:** run Python and JavaScript linters and suggest patches

## Docs Agent
- **When:** docs or README change
- **Does:** spell‑check and link‑check

## Release Drafter
- **When:** commits land on `main`
- **Does:** update release notes automatically

## Prompt Agent
- **When:** you run `flywheel prompt`
- **Does:** generate context‑aware prompts for Codex or other LLMs

Run `pre-commit run --all-files` before pushing. This executes `./run_all_tests.sh` and mirrors CI.
