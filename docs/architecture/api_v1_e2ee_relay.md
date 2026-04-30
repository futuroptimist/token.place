# API v1-only E2EE relay architecture (v0.1.0)

This note is the canonical architecture baseline for API v1 relay-blind E2EE.

## Release target and scope

- **API v1 is the active API for token.place v0.1.0.**
- **API v1 is non-streaming.** Responses are returned only after full model generation is
  complete.
- **Do not add streaming to API v1** for relay/client-server inference paths.

## Runtime routing rules (must-follow)

All active production inference paths must use API v1 E2EE routes:

- `server.py` API/runtime inference paths
- `relay.py` relay paths
- `client.py` client paths
- `desktop-tauri` compute-node / bridge paths
- relay landing-page HTML chat UI served by `relay.py`

If a path cannot preserve API v1 E2EE invariants, it must **fail closed** instead of routing
plaintext or using deprecated fallbacks.

## API v2 status

- API v2 exists in the repository, but it is currently incomplete.
- Do **not** route active runtime traffic through API v2 yet.
- Do **not** migrate server, relay, client, desktop, or relay HTML chat UI runtime paths to API v2
  until API v1 is launched and v0.1.0 is finalized.

## Deprecated legacy relay endpoints

The following endpoints are deprecated legacy relay routes:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

Rules:

- Do not use them in active production inference paths.
- Do not extend them for new features.
- Do not reintroduce them as compatibility fallbacks in active runtime traffic.
- Use API v1 E2EE relay routes instead.

Legacy routes may remain temporarily for historical compatibility and migration staging, but they
must be clearly labeled deprecated legacy behavior in docs and code comments.

## E2EE invariant (relay-blind requirement)

Relay-visible surfaces must remain ciphertext-only plus safe routing metadata.

Relay-owned state, relay logs, relay diagnostics, and relay HTTP payloads must never include
plaintext model payload content, including:

- plaintext prompts
- OpenAI `messages`
- legacy `prompt` fields
- assistant response text
- tool arguments
- model output text or equivalent content payloads

Any path that would expose plaintext to relay-owned surfaces must fail closed.

## Local developer workflow

1. Start relay (`python relay.py`) and confirm relay diagnostics endpoint is reachable:
   `/relay/diagnostics`.
2. Start desktop bridge/compute node and ensure it registers on API v1 relay routes:
   `/api/v1/relay/servers/register` and `/api/v1/relay/servers/poll`.
3. Open landing-page chat and verify requests flow through API v1 E2EE routes:
   `/api/v1/relay/servers/next` -> `/api/v1/relay/requests` ->
   `/api/v1/relay/responses` -> `/api/v1/relay/responses/retrieve`.

## Success signals and failure signatures

- Success:
  - relay diagnostics shows registered compute nodes with routing metadata only.
  - browser chat returns non-stub assistant content through API v1 path.
- Failure signatures:
  - local provider selected when desktop bridge intended.
  - heartbeat-only compute node (registers but never polls).
  - any request to legacy endpoints (`/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`).
  - `stub` content in chat response.
  - any `E2EE_SENTINEL_*` plaintext marker in relay logs/diagnostics/state.
