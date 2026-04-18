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

## Hardware acceleration (`llama_cpp_python`)

- Follow [`README.md#hardware-acceleration`](README.md#hardware-acceleration)
  when enabling desktop/server GPU support.
- On Windows + NVIDIA, run these in the same virtualenv used by token.place:
  - Command Prompt:
    - `set CMAKE_ARGS=-DGGML_CUDA=on`
    - `set FORCE_CMAKE=1`
  - PowerShell:
    - `$env:CMAKE_ARGS="-DGGML_CUDA=on"`
    - `$env:FORCE_CMAKE=1`
  - Then reinstall:
    - `pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose`
- Keep `n_gpu_layers` non-zero (typically `-1` for full offload) when GPU/hybrid
  execution is expected.
