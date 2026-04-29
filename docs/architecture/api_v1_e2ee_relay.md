# API v1-only E2EE relay architecture (v0.1.0 baseline)

## Status and scope (Prompt 0 baseline)

- **API v1 is the active API for token.place v0.1.0.**
- **API v1 is non-streaming.** Responses are returned only after the full model response is generated.
- **Do not add streaming to API v1.**
- **API v2 exists but is incomplete.** Do not route active runtime traffic through API v2 yet.
- Do not migrate UI, desktop, relay, or server runtime flows to API v2 until API v1 is fully launched and v0.1.0 is finalized.

## Required active-path alignment

All active runtime inference paths must use API v1 E2EE relay routes:

- `server.py`
- `relay.py`
- `client.py`
- desktop Tauri app
- relay landing-page HTML chat UI (`relay.py` + `static/index.html` + `static/chat.js`)

## Legacy relay endpoints (deprecated)

The following endpoints are **deprecated legacy relay routes**:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

Rules for active production paths:

- Do not use these routes in active production traffic.
- Do not extend these routes for new features.
- Do not reintroduce these routes as compatibility fallbacks in new code.
- Use API v1 E2EE relay routes instead.

Legacy routes may remain temporarily for historical compatibility or migration staging, but they are legacy-only and must be clearly labeled as such in docs and code comments.

## E2EE invariant (must hold)

Relay-path inference must remain relay-blind E2EE:

- Relay sees ciphertext plus safe routing metadata only.
- Relay-owned state, relay logs, relay diagnostics, and relay-visible HTTP payloads must never contain plaintext model payload content.
- This includes prompts, OpenAI-style `messages`, legacy `prompt`, assistant responses, tool arguments, and model output text.
- If a path cannot preserve E2EE, it must fail closed.

## Why this baseline exists

As of this documentation baseline, there is a known alignment gap between `relay.py`, the desktop Tauri app, and the relay HTML chat UI. Some end-to-end flow segments still touch legacy routes.

Prompt sequence intent:

- Prompt 0 (this docs baseline): lock in architecture intent.
- Prompts 1-4: restore route contracts, migrate active callers, remove legacy-path usage from active runtime flows, and add final guardrails.

This document is evergreen guidance for contributors and coding agents so legacy route usage is not accidentally reintroduced.
