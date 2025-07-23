# \U0001F916 AGENTS

This project uses several lightweight LLM assistants to keep the flywheel spinning.
See [llms.txt](llms.txt) for a quick orientation summary and [CLAUDE.md](CLAUDE.md)
for Anthropic-specific coding guidance. Broader Codex behavior rules live in
[CUSTOM_INSTRUCTIONS.md](CUSTOM_INSTRUCTIONS.md).

## Code Linter Agent
- **When:** every PR
- **Does:** run ESLint/Flake8 and suggest patches

## Docs Agent
- **When:** docs or README change
- **Does:** spell-check and link-check, suggest style tweaks

## Quest Generator Agent
- **When:** you request a new quest
- **Does:** scaffold metadata, code stubs, and tests

## Synergy Bot
- **When:** multiple repos evolve
- **Does:** detect duplicate utilities and propose extraction into a shared package
- Works well with [Axel](https://github.com/futuroptimist/axel) for coordinating quests across repositories

## Release Drafter Bot
- **When:** commits land on `main`
- **Does:** update release notes automatically

## Prompt Agent
- **When:** you run `flywheel prompt`
- **Does:** generate context-aware prompts for Codex or other LLM assistants

---

For personalization, run `./scripts/setup.sh YOURNAME YOURREPO` after cloning.

Before pushing changes, run `pre-commit run --all-files` to execute the same
checks used in CI.
