# `token.place` Onboarding Guide

Welcome to `token.place`! This document provides a high-level overview of the project structure and pointers for where to learn more.

## Repository Layout

- **`config.py`** – central configuration system that detects your platform and sets up paths for models, data, cache and logs.
- **`server/`** – code for the LLM server. It downloads a model if needed, polls the relay for messages, decrypts them, generates a response and sends it back.
- **`relay.py`** – relay server that forwards encrypted requests between clients and servers while keeping IP addresses hidden.
- **`api/`** – OpenAI-compatible API implementation. It also supports optional end-to-end encryption.
- **`utils/`** – helpers for encryption (`crypto_helpers.py`), model management and networking utilities.
- **`static/`** – frontend assets including `chat.js`, a browser-based chat client that performs client-side encryption.
- **`tests/`** – unit, integration and end-to-end tests. See [tests/README.md](../tests/README.md).

## Encryption Workflow

1. Clients generate an RSA key pair and fetch the server's public key.
2. Each message is encrypted with a random AES key and IV. The AES key is encrypted with the server's RSA key.
   AES-CBC remains the default, but AES-GCM can be enabled when authenticated encryption is needed.
3. The server decrypts the AES key, forwards the encrypted message to the LLM and returns an encrypted result.
4. The client decrypts the response with its AES key.

This ensures that plaintext prompts and responses never reach the relay or any intermediate service. See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for more details.


## API v1-only architecture baseline (v0.1.0)

- API v1 is the active API for `v0.1.0` and the only active runtime integration target.
- API v1 is non-streaming for relay/client-server inference: return responses only after full
  generation is complete.
- Do not add streaming to API v1.
- API v2 exists but is incomplete; do not route runtime traffic through API v2 yet.
- Deprecated legacy relay endpoints (`/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`)
  must not be used in active production paths, extended for new features, or revived as fallbacks.
- Active inference paths for `server.py`, `relay.py`, `client.py`, desktop Tauri, and relay HTML
  chat UI should align on API v1 E2EE routes.
- Relay must remain ciphertext-only plus safe routing metadata. If a path cannot preserve E2EE,
  it must fail closed.

Known migration context: `relay.py`, desktop Tauri, and relay HTML chat UI are not fully aligned
yet and some E2E pieces still touch legacy routes. Prompts 1-4 own that migration work.
See [docs/architecture/api_v1_e2ee_relay.md](architecture/api_v1_e2ee_relay.md).

## Cross-Platform Notes

The project runs on Windows, macOS and Linux. Path handling, configuration locations and launcher scripts adapt automatically. For platform specifics and Docker instructions, see [CROSS_PLATFORM.md](CROSS_PLATFORM.md).

The `.gitignore` file keeps OS artifacts like `.DS_Store` and `Thumbs.db` out of commits.

## Where to Go Next

- **Architecture** – [docs/ARCHITECTURE.md](ARCHITECTURE.md)
- **Testing** – [docs/TESTING.md](TESTING.md) and [tests/README.md](../tests/README.md)
- **Contribution guidelines** – [CONTRIBUTING.md](../CONTRIBUTING.md)
- **Style guide** – [docs/STYLE_GUIDE.md](STYLE_GUIDE.md)
- **LLM assistant tips** – [docs/LLM_ASSISTANT_GUIDE.md](LLM_ASSISTANT_GUIDE.md)
- **Automation guidelines** – [AGENTS.md](AGENTS.md)

## Local checks

Set up development dependencies and run the full test suite before pushing changes:

```bash
pip install pre-commit
npm ci
pip install -r config/requirements_server.txt
pip install -r config/requirements_relay.txt
pip install -r requirements.txt
npm run lint
npm run type-check
npm run build
npm run test:ci
pytest -q tests/test_security.py
bandit -r . -lll
pre-commit run --all-files
```

Development dependencies live in [requirements.txt](../requirements.txt).

`npm run lint` executes ESLint over the TypeScript client sources and their test harnesses, so
static analysis failures surface before the broader CI workflow runs.

Happy hacking!
