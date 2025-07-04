# 🤖 AGENTS

This repository uses lightweight LLM agents to automate routine tasks and keep the project healthy.

- **Code Linter Agent** – runs lint and format checks on every pull request.
- **Docs Agent** – checks documentation for spelling and link issues.
- **Quest Generator Agent** – scaffolds new quests when requested.
- **Synergy Bot** – proposes shared utilities across related repos.
- **Release Drafter Bot** – updates release notes as commits land on `main`.
- **Prompt Agent** – generates context-aware prompts.

Before pushing changes:

1. Run `./run_all_tests.sh`.
2. Run `pre-commit run --all-files`.

See [docs/AGENTS.md](docs/AGENTS.md) for full details and [CLAUDE.md](CLAUDE.md) for Claude-specific guidance.
