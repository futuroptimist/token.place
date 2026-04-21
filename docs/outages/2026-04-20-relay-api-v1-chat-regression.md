# Outage report: relay landing-page chat API v1 regression (2026-04-20)

## Summary

The relay-served landing chat at `/` regressed after partial API migration work. Browser chat
attempts failed with an API v1 error path that surfaced as a generic streaming error in the UI,
then a backend `Failed to encrypt response` failure when the request contract drifted.

## User-visible impact

- Landing chat showed `Streaming chat completion failed: Error: Unknown streaming error`.
- `POST /api/v1/chat/completions` returned HTTP 500 in affected flows.
- API responses included `Failed to encrypt response`.
- Users could load the page, but could not complete a chat round trip.

## Guardrail violated

This violated `docs/AGENTS.md` `v0.1.0` guardrails:

- Relay landing-page chat path is **API v1-only** (`static/index.html` + `static/chat.js`).
- Relay-path traffic is **non-streaming by design**.
- `relay.py`, `server.py`, and desktop-tauri API/network behavior must stay aligned on API v1.

## Root cause

A contract mismatch slipped in across layers:

1. The landing-page chat flow retained logic consistent with streaming-style error assumptions even
   though relay-path API v1 is non-streaming.
2. API v1 encrypted request/response handling drifted from expected contract shape in parts of the
   browser path, causing encryption to fail and return `Failed to encrypt response`.
3. The UI surfaced fallback messaging that obscured the actual API v1 failure reason.

## Why CI did not catch this earlier

Existing tests covered pieces in isolation (UI behavior with mocked routes, API behavior, relay
behavior, and desktop runtime behavior), but did not include a single browser-driven round trip
asserting **live relay + live compute-node server runtime + landing page UI** with API v1-only
routing and non-streaming expectations in one test.

## Remediation in this fix

- Kept landing-page relay chat on API v1 only and non-streaming behavior.
- Tightened browser-side API v1 error handling so real backend failures are surfaced clearly.
- Added browser e2e coverage that uses live relay + live `server.py` runtime wiring and asserts:
  - UI request goes to `/api/v1/chat/completions`.
  - No `/api/v2/chat/completions` request is made.
  - The assistant response is rendered end-to-end.

## Regression tests added

- `tests/e2e/test_ui.py::test_landing_chat_live_relay_server_round_trip_api_v1`

## Follow-up actions

- Keep relay-path landing chat tests as API v1-only and non-streaming contract tests.
- Keep desktop-tauri parity checks anchored to shared compute runtime behavior to prevent API
  contract drift between `server.py` and desktop compute-node flows.
