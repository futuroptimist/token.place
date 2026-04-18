# \U0001F916 AGENTS

token.place uses a set of helpers to automate common tasks. This file follows the
[Agents.md specification](https://agentsmd.net/) so LLMs know how to cooperate
with the repo. A plain-text mirror lives in [llms.txt](llms.txt).

## Setup
- Run `./scripts/setup.sh YOURNAME YOURREPO` after cloning to personalize local
  configs.

## Checks
- Always run `pre-commit run --all-files` before pushing. This executes
  `./run_all_tests.sh` and mirrors CI.

## Hardware acceleration (llama-cpp-python)
- Keep the desktop and server GPU runtime behavior aligned with the
  [README hardware acceleration section](README.md#hardware-acceleration).
- Windows/NVIDIA support depends on installing a CUDA-enabled
  `llama-cpp-python` build (not CPU-only wheels) with `CMAKE_ARGS=-DGGML_CUDA=on`
  and `FORCE_CMAKE=1` when source-building is required.
- macOS Apple Silicon support depends on Metal-enabled `llama-cpp-python`
  builds as documented in the README.
- When touching desktop runtime/bootstrap code, preserve explicit diagnostics
  (`backend_available`, `backend_used`, `llama_module_path`, fallback reason) so
  GPU fallbacks are auditable in logs and tests.

## Coding Conventions
- New JavaScript should be written in **TypeScript** using functional React
  components and hooks.
- Use **Tailwind CSS** for styling and keep custom CSS minimal.

## Agents

### Code Linter Agent
- **When:** every PR
- **Does:** run ESLint and Flake8, suggesting patches

### Docs Agent
- **When:** docs or README change
- **Does:** spell-check and link-check, plus style tweaks

### Quest Generator Agent
- **When:** you request a new quest
- **Does:** scaffold metadata, code stubs, and tests

### Synergy Bot
- **When:** multiple repos evolve
- **Does:** detect duplicate utilities and propose extraction into a shared package
- Works with [Axel](https://github.com/futuroptimist/axel) to coordinate quests

### Release Drafter Bot
- **When:** commits land on `main`
- **Does:** update release notes automatically

### CI Bot
- **When:** pushes or PRs
- **Does:** run tests via GitHub Actions

### Claude PR Assistant
- **When:** you mention `@claude` in a PR or issue
- **Does:** analyzes your code changes and proposes patches

### Security Bot
- **When:** dependency or vulnerability alerts appear
- **Does:** open PRs to patch them

### Prompt Agent
- **When:** you run `flywheel prompt`
- **Does:** generate context-aware prompts for Codex or other LLM assistants
