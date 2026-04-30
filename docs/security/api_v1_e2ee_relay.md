# API v1 E2EE relay architecture and legacy endpoint deprecation

## Invariant

Distributed relay traffic is relay-blind E2EE: relay-owned state and logs contain only ciphertext plus safe routing metadata.

## Active API v1 routes

- `POST /api/v1/relay/servers/register`
- `POST /api/v1/relay/servers/poll`
- `GET /api/v1/relay/servers/next`
- `POST /api/v1/relay/requests`
- `POST /api/v1/relay/responses`
- `POST /api/v1/relay/responses/retrieve`

## Deprecated legacy routes

The following endpoints are deprecated and disabled by default, returning HTTP 410 unless
`TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES=1` is explicitly set for compatibility testing:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

## Local workflow

1. Start relay.
2. Start desktop bridge compute node and confirm register + poll heartbeat.
3. Open landing-page chat and send a prompt.
4. Verify relay request/response path uses API v1 routes above and ciphertext envelopes.

## Common failure signatures

- Local provider selected when desktop bridge expected.
- Heartbeat-only compute node with no completed responses.
- Legacy endpoint usage returns `legacy_relay_endpoint_deprecated`.
- Any `stub` response indicates a regression.
- Any `E2EE_SENTINEL_*` plaintext in relay logs/state indicates E2EE regression.
