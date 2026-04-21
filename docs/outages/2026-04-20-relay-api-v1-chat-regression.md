# Outage report: relay landing chat API v1 regression

- **Date detected:** April 20, 2026
- **Affected surface:** relay landing page chat (`/` via `static/index.html` + `static/chat.js`)
- **User-visible impact:** browser chat failed with API errors and no assistant response.

## What broke

The relay landing-page browser chat regressed after a partial migration: the UI/contract path drifted
from the API v1 encrypted response expectations used by `api/v1/routes.py`.

## User-visible symptoms

Users reported all three of the following while sending chat messages from the relay landing page:

- `Streaming chat completion failed: Error: Unknown streaming error`
- `POST /api/v1/chat/completions 500 (INTERNAL SERVER ERROR)`
- `API error: Failed to encrypt response`

The chat UI then rendered a generic assistant-side failure message instead of a model response.

## Guardrail violated

`docs/AGENTS.md` defines a `v0.1.0` boundary that was violated:

- relay path traffic is **API v1-only** for:
  - desktop-tauri network/API calls,
  - `relay.py`,
  - relay landing-page chat (`static/index.html` + `static/chat.js`),
  - `server.py` API traffic.
- relay-path traffic is **non-streaming by design** for this release.

## Root cause

Two contract drifts combined:

1. The landing-page flow still had streaming/v2 assumptions in the user journey, while relay-path
   chat must be non-streaming API v1 in `v0.1.0`.
2. The UI and API v1 encryption contract diverged for the `client_public_key`/response handling path,
   which caused API v1 response encryption to fail (`Failed to encrypt response`).

## Why CI did not catch it earlier

Coverage existed for isolated pieces, but not for the complete wiring path from:

1. relay-served browser UI,
2. API v1 encrypted request/response handling,
3. relay routing, and
4. desktop compute-node bridge runtime.

Without that end-to-end relay+desktop+browser assertion, contract drift across boundaries was not
caught quickly.

## Fix implemented

- Enforced API v1 non-streaming behavior for the relay landing chat path.
- Kept encrypted API v1 request/response parsing aligned with backend expectations.
- Added an end-to-end browser test that validates the full relay + desktop bridge wiring and fails
  if the landing path uses API v2 streaming.

## Regression tests added

- `tests/e2e/test_ui.py::test_landing_chat_uses_api_v1_only_non_streaming`
  - Guards that landing chat stays on `/api/v1/chat/completions` and avoids `/api/v2`/streaming.
- `tests/e2e/test_ui.py::test_landing_chat_e2e_round_trip_via_relay_and_desktop_bridge`
  - Starts relay + desktop compute-node bridge path, sends a browser message from `/`, and verifies
    successful assistant response round-trip.

## Follow-up actions

1. Keep the new relay+desktop+browser e2e test in the default CI path.
2. Treat API contract changes that touch encryption fields as outage-sensitive changes requiring
   integration coverage updates in the same PR.
3. Keep `docs/AGENTS.md` as the single source of truth for release guardrails.
