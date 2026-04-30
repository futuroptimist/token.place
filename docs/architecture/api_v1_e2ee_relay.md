# API v1-only E2EE relay architecture (v0.1.0)

## Architecture summary

token.place v0.1.0 uses API v1 as the only active runtime API for relay-based inference.
Distributed inference must remain relay-blind end-to-end encryption (E2EE): the relay can only
handle ciphertext plus safe routing metadata.

## Active API v1 relay flow

1. Compute node registers and heartbeats with `POST /api/v1/relay/servers/register`.
2. Client/server API v1 provider fetches a target node with `GET /api/v1/relay/servers/next`.
3. Client/server API v1 provider submits encrypted request envelope to
   `POST /api/v1/relay/requests`.
4. Compute node polls for encrypted work with `POST /api/v1/relay/servers/poll`.
5. Compute node returns encrypted response envelope with `POST /api/v1/relay/responses`.
6. Client/server API v1 provider retrieves encrypted response from
   `POST /api/v1/relay/responses/retrieve`.

## Deprecated legacy endpoints

The following legacy endpoints are deprecated and must not be used by active production inference
paths:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

Current behavior: these routes remain in `relay.py` only to return explicit deprecation errors
(HTTP 410) that point callers to API v1 endpoints.

## E2EE invariant

The relay must never store, log, or return plaintext model payload content. This includes prompts,
`messages`, assistant text, tool arguments, and model output text. If a path cannot preserve this,
it must fail closed.

## Local developer workflow

1. Start relay: `python relay.py`.
2. Start compute node bridge runtime (desktop-tauri flow) configured for API v1 relay endpoints.
3. Open relay landing-page chat UI in browser and run chat inference.
4. Validate relay diagnostics and logs for ciphertext-only behavior.

## Expected success signals

- Relay returns 200 from API v1 relay routes listed above.
- Compute node poll returns queued encrypted envelopes.
- Relay diagnostics show registered compute nodes without plaintext payload fields.
- No relay logs include message plaintext.

## Common failure signatures

- **Local provider selected unexpectedly**: API v1 route returns local backend marker instead of
  distributed relay marker.
- **Heartbeat-only compute node**: node appears registered but polling endpoint returns no queued
  work while requests are expected.
- **Legacy endpoint usage**: route returns 410 deprecation error for `/sink`, `/faucet`,
  `/source`, `/retrieve`, or `/next_server`.
- **Stub response regression**: UI reports success without compute-node response payload.
- **Plaintext sentinel regression**: sentinel strings appear in relay state, diagnostics, logs,
  outbound relay calls, or API error payloads.
