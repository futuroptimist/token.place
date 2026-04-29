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

## Codex focused verification bootstrap
- For Codex follow-up verification tasks, install focused dependencies with
  `pip install -r config/requirements_codex_verification.txt`.
- To bootstrap in one step (including Playwright Chromium), run
  `./scripts/bootstrap_focused_verification.sh`.
- This codex-specific requirements surface is intended for verification tasks
  and complements (does not replace) the standard project dependency setup.

## Desktop GPU acceleration (llama_cpp_python)
- For desktop-tauri GPU modes (`auto`, `gpu`, `hybrid`), treat GPU-capable
  `llama-cpp-python` builds as a release requirement, not an optimization.
- Follow the hardware acceleration section in `README.md` when (re)building
  local runtimes:
  - **Windows + NVIDIA/CUDA:** ensure CUDA-enabled builds (`CMAKE_ARGS=-DGGML_CUDA=on`,
    `FORCE_CMAKE=1`) and verify runtime reports CUDA usage.
  - **macOS Apple Silicon:** ensure Metal-enabled builds
    (`CMAKE_ARGS=-DGGML_METAL=on`) and verify runtime reports Metal usage.
- For desktop changes that touch Python runtime bootstrapping, packaging, or
  sidecar launch, include regression coverage that asserts GPU mode does not
  silently fall back to CPU when GPU runtime support is expected.


## Relay-blind E2EE invariant (must-follow)
- Distributed inference relay traffic is ciphertext-only; relay-visible plaintext model payload content is forbidden.
- Never queue, log, diagnose, forward, or echo OpenAI `messages`/`prompt` plaintext through relay-owned state or relay-targeted network calls.
- If distributed relay cannot satisfy relay-blind E2EE, fail closed.

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
