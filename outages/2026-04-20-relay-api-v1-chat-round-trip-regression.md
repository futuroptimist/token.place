# Outage: relay landing-page API v1 chat wiring regressed across relay/browser/desktop paths

- **Date:** 2026-04-20
- **Slug:** `relay-api-v1-chat-round-trip-regression`
- **Affected area:** relay-served landing-page chat path (`/`) using API v1

## Summary
The landing-page browser chat regressed because API contract drift was introduced across the relay
UI path and encryption expectations. The browser tried a streaming-oriented path and key encoding
shape that did not match the API v1 non-streaming encryption contract used by relay compute nodes.

## User-visible symptoms
- Browser console reported: `Streaming chat completion failed: Error: Unknown streaming error`.
- `POST /api/v1/chat/completions` returned `500 (INTERNAL SERVER ERROR)`.
- API body included `Failed to encrypt response`.
- Chat UI displayed a generic failure message instead of a usable assistant response.

## Guardrail violated
`docs/AGENTS.md` explicitly requires, for `v0.1.0`:
- API v1-only traffic for desktop-tauri network calls, `relay.py`, landing chat
  (`static/index.html` + `static/chat.js`), and `server.py`.
- relay-path traffic must be **non-streaming** (no SSE/v2 flow for landing chat).

## Root cause
1. Landing chat implementation drifted toward streaming assumptions that are invalid for relay-path
   `v0.1.0` traffic.
2. Browser/response contract drift around encrypted API v1 payload expectations caused key-format
   mismatch during response encryption.
3. CI coverage around the landing chat flow did not validate the full integration boundary through
   the desktop bridge runtime and relay sink/source contract.

## Why CI did not catch this sooner
The existing landing-page Playwright smoke test mocked `/api/v1/chat/completions`. That verified
frontend rendering and endpoint selection but did not prove end-to-end relay wiring with the
desktop compute-node bridge process that participates in the real sink/source relay contract.

## Remediation in this change
- Kept landing chat behavior pinned to API v1, non-streaming flow.
- Added CI-grade e2e coverage that runs:
  browser UI -> `relay.py` API v1 route -> relay queue/sink -> desktop-tauri
  `compute_node_bridge.py` runtime -> relay source -> UI render.
- Extended focused relay-e2e fixture selection so this regression test can run in CI without
  enabling the broader relay registration suite.

## Recurrence prevention
- Keep relay landing-page tests asserting API v1/non-streaming behavior.
- Keep at least one real relay + desktop bridge + browser round-trip e2e test unmocked at the
  `/api/v1/chat/completions` boundary.
- Treat any client key-serialization or encrypted response-shape changes as outage-class changes
  requiring full-path e2e coverage updates.

