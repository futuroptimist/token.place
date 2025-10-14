# token.place repository map

This guide orients new contributors to the key directories and configuration
files. It complements the high-level tour in [docs/README.md](README.md) and the
hands-on walkthrough in [ONBOARDING.md](ONBOARDING.md).

## Applications

- `server/` — Flask app exposing the OpenAI-compatible API v1 and v2 endpoints.
  - Pulls shared helpers from `utils/` and configuration from `config.py`.
- `api/` — FastAPI implementation of the modern API surfaces.
  - Mirrors the v1 routes while hosting experimental adapters under `api/v2/`.
- `relay.py` / `relay/` — Lightweight relay that forwards encrypted traffic.
  - Reads relay settings from `config/requirements_relay.txt` and `.env` files.
- `server.py` — Convenience entrypoint that wires the relay and model runtime.
  - Honours feature toggles such as `CONTENT_MODERATION_MODE`.

## Client experiences

- `static/` — Browser assets (HTML, JS, CSS) that exercise the encrypted chat flow.
  - `static/chat.js` provides the reference JavaScript crypto client.
- `desktop/` — Electron desktop application with the same encrypted UX.
  - Uses `npm` scripts defined in the workspace `package.json`.
- `client.py` — Rich terminal client with logging, streaming, and fallback behaviour.
  - `client_simplified.py` offers a minimal variant for demos.

## Shared libraries

- `utils/` — Reusable Python helpers for crypto, rate limiting, and config loading.
- `dict/` — Data files and blocklists referenced by moderation and routing logic.
  - Keep sensitive allow or block lists encrypted when stored outside this repo.

## Configuration and operations

- `config/` — Environment-specific configuration and dependency pins.
  - `requirements_server.txt` and `requirements_relay.txt` split dependencies.
- `config.py` — Central configuration loader for relay and server processes.
  - Reads `.env`, `.env.local`, and CLI overrides.
- `docker/`, `docker-compose.yml` — Container images and compose definitions.
  - `infra` consolidation is tracked in the polish roadmap.
- `k8s/` — Kubernetes manifests for cluster deployments.
  - Aligns with Raspberry Pi notes in `RPI_DEPLOYMENT_GUIDE.md`.
- `scripts/` — Operational helpers covering setup, testing, and doc sync.
  - `run_all_tests.sh` is called by CI and pre-commit hooks.

## Testing

- `tests/` — Python and Playwright suites covering crypto compatibility and API flows.
  - See [TESTING.md](TESTING.md) for markers and execution guidance.
- `run_all_tests.sh` — Aggregated runner invoked locally and in CI.
  - Wraps pytest, Playwright, npm checks, and Bandit.

## Documentation resources

- [README.md](../README.md) &rarr; top-level quickstart and CI requirements.
- [docs/ONBOARDING.md](ONBOARDING.md) &rarr; guided setup narrative.
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) &rarr; architectural deep dive.
- [docs/TESTING.md](TESTING.md) &rarr; detailed coverage of automated suites.
- [docs/STYLE_GUIDE.md](STYLE_GUIDE.md) &rarr; branding and writing guidance.

Contributions that move files should update this map so future maintainers always
have an accurate snapshot of the workspace layout.
