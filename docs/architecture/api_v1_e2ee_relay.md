# API v1-only E2EE relay architecture (v0.1.0)

This document defines the active runtime architecture for distributed inference in token.place.

## Release target

- API v1 is the active API for token.place v0.1.0.
- API v1 relay inference is non-streaming by design.
- API v2 exists in-repo but is not a runtime target yet.

## Active distributed flow

1. Client fetches next compute node via `GET /api/v1/relay/servers/next`.
2. Client submits encrypted request envelope via `POST /api/v1/relay/requests`.
3. Compute node polls encrypted work via `POST /api/v1/relay/servers/poll`.
4. Compute node posts encrypted response envelope via `POST /api/v1/relay/responses`.
5. Client retrieves encrypted response via `POST /api/v1/relay/responses/retrieve`.

Relay routing metadata is allowed; plaintext model content is not.

## Deprecated legacy endpoints

The following relay endpoints are deprecated and must not be used by active production inference:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

Compatibility behavior must be explicit and temporary. New code must use API v1 E2EE relay routes.

## E2EE invariant

Relay-owned state, diagnostics, logs, and relay-targeted payloads must remain ciphertext-only
plus safe routing metadata. If a path cannot preserve relay-blind E2EE, it must fail closed.

## Local developer workflow

- Start relay: `python relay.py`
- Start compute node bridge/server path configured for API v1 relay routes.
- Open relay landing page and send a chat message.
- Verify successful handshake and encrypted request/response flow through API v1 relay routes.

## Common failure signatures

- Local provider selected unexpectedly when distributed bridge was intended.
- Compute node heartbeats present, but no work claims from `/api/v1/relay/servers/poll`.
- Any attempt to call deprecated legacy endpoints.
- `stub` response in place of real encrypted relay response.
- Plaintext sentinel strings appearing in relay logs/diagnostics/state.
