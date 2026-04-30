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
- Distributed relay inference must be relay-blind E2EE (ciphertext only + routing metadata).
- Never queue, forward, log, diagnose, or expose plaintext model payload content in relay-owned state.
- Any distributed plaintext path must fail closed unless replaced by an approved E2EE envelope path.


## API v1-only relay architecture guardrails (migration baseline)
- API v1 is the active API for `v0.1.0` and the only active runtime target.
- API v1 is non-streaming; responses are returned only after full model output generation.
- Do not add streaming to API v1 relay/client-server inference paths.
- API v2 exists but is incomplete; do not route runtime traffic through API v2 until API v1 is
  launched and `v0.1.0` is finalized.
- Deprecated legacy relay endpoints: `/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`.
  Do not use them in active production code, do not extend them, and do not reintroduce them as
  compatibility fallbacks.
- Active runtime inference paths for `server.py`, `relay.py`, `client.py`, desktop Tauri flows,
  and relay landing-page HTML chat UI must align on API v1 E2EE routes.
- Relay-owned state/logs/diagnostics/payloads must stay ciphertext-only (+ safe routing metadata).
  Never expose plaintext prompts/messages/responses/tool arguments/model output text.
- If E2EE cannot be preserved for a path, fail closed.
- Context: there is a known migration gap where some E2E pieces still touch legacy
  routes; migrations are intentional follow-up work, not behavior to preserve.
- Architecture note: [docs/architecture/api_v1_e2ee_relay.md](docs/architecture/api_v1_e2ee_relay.md).

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
