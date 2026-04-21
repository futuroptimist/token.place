# Outage: relay landing-page chat regression from API v1 contract drift

- **Date:** 2026-04-20
- **Severity:** user-visible functional outage
- **Affected areas:**
  - `relay.py` landing-page UI at `/`
  - API v1 encrypted chat response handling
  - relay compute-node compatibility between `server.py` and desktop bridge

## What broke

The relay landing-page browser chat regressed after partial migration work and
started following streaming-oriented assumptions that were incompatible with the
`v0.1.0` API v1 guardrails.

## User-visible symptoms

- Browser console showed: `Streaming chat completion failed: Error: Unknown streaming error`.
- `POST /api/v1/chat/completions` returned HTTP `500`.
- API payload surfaced `Failed to encrypt response`.
- UI showed fallback assistant text instead of a real response.

## Guardrail violated

Per `docs/AGENTS.md` (`v0.1.0` boundaries):

- relay-path traffic must be **API v1-only** for:
  - `relay.py`,
  - `server.py`,
  - desktop-tauri API traffic,
  - relay landing-page UI (`static/index.html` + `static/chat.js`).
- relay landing-page traffic is **non-streaming by design**.

The regression violated this by allowing stream-oriented contract assumptions to
leak into relay-path handling.

## Root cause

Two drifts combined:

1. **Landing-page contract drift:** browser flow and error handling reflected
   streaming expectations that are invalid for relay `v0.1.0` API v1 flow.
2. **Compute-node parity drift:** shared relay compute handling still accepted
   stream hints and could branch to `/stream/source`, diverging from the
   non-streaming API v1 relay guardrail used by `server.py` and desktop bridge.

Under these conditions, encrypted response handling hit mismatched assumptions
and surfaced `Failed to encrypt response`.

## Why CI did not catch it earlier

- Existing landing-page API v1 tests validated endpoint selection but did not
  assert a real desktop-bridge relay round trip.
- Existing desktop/relay tests validated stream-capable legacy behavior but did
  not enforce the `v0.1.0` non-streaming relay-path rule across shared runtime.

## Remediation shipped

1. Enforced non-streaming behavior in shared relay client request processing:
   stream hints are ignored and requests always return through `/source`.
2. Updated desktop-bridge and relay-client regression tests to lock this
   non-streaming behavior.
3. Added end-to-end coverage for relay + desktop bridge encrypted round trips via
   `/faucet` and `/retrieve`, plus landing-page UI API v1-only assertions.

## New regression coverage

- `tests/unit/test_relay_client.py`:
  - stream hint fallback to `/source`
  - registration token header retention on stream-hinted requests
- `tests/unit/test_desktop_compute_node_bridge.py`:
  - stream-hinted payloads still process through shared non-streaming relay path
- `tests/test_relay_desktop_bridge_e2e.py`:
  - encrypted relay round trip using desktop compute-node bridge runtime
- `tests/e2e/test_ui.py`:
  - explicit no `/api/v2/chat/completions` traffic assertion for landing-page chat

## Follow-up actions

1. Keep relay-path stream handling behind post-`v0.1.0` gating only.
2. Preserve a single API v1 response contract for browser, server, and desktop paths.
3. Treat any reintroduction of stream hints on relay landing flow as release-blocking.
