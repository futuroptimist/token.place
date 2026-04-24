# Outage: relay landing-page desktop-bridge API v1 mismatch

- **Date:** 2026-04-20
- **Slug:** `relay-landing-page-desktop-bridge-api-v1-mismatch`
- **Affected area:** relay landing-page chat (`/`) with desktop-tauri operator bridge

## Summary
The landing-page UI sent requests to `/api/v1/chat/completions`, but the desktop bridge was only
participating in legacy relay poll/response handling. This created a split-brain path where the
desktop app could appear healthy and registered while landing-page API v1 requests were not served
by the registered desktop compute node.

## User-visible symptoms
- Landing chat could return a fake/stub reply instead of real desktop inference.
- Failure messaging was weak when no compute nodes were actually available for the API v1 route.
- Operators could see a healthy/registered desktop status that did not correspond to landing-page
  request handling.

## Impact
Users were given misleading success signals: the desktop operator looked online, but landing-page
chat quality and error handling did not reflect real API v1 desktop-bridge execution.

## Root cause
1. Protocol mismatch: landing-page traffic used API v1 chat completions while the desktop bridge
   logic under test was effectively validated through legacy relay behavior.
2. Provider-path ambiguity allowed local/provider bypass behavior to satisfy tests without proving
   desktop-bridge API v1 execution.

## Contributing factors
- The previous smoke/e2e composition relied on mocked `/api/v1/chat/completions` coverage for key
  UI checks.
- Guardrails did not consistently force distributed API v1 routing with fallback disabled during
  the real desktop-bridge path.
- The test setup could still look healthy from registration-only signals without proving request
  processing by `compute_node_bridge.py`.

## Why CI/tests missed it
CI validated a misaligned guardrail: it confirmed the UI and route usage while allowing the
local-provider bypass path, so it did not reliably assert that real landing-page API v1 requests
were processed by the registered desktop bridge.

## Resolution
- Added a real, unmocked relay landing-page e2e guardrail test:
  `tests/e2e/test_ui.py::test_landing_chat_real_inference_with_desktop_bridge_api_v1`.
- Updated relay e2e fixture guardrails in `tests/conftest.py` so this flow enforces distributed
  API v1 routing and disables distributed fallback/local bypass in real bridge mode.
- Made CI run this guardrail explicitly with a tiny real GGUF model in
  `.github/workflows/ci.yml` before the broader suite.
- Kept landing-page behavior pinned to API v1 non-streaming and strengthened assertions that
  desktop bridge request processing occurred (not just registration heartbeat state).

## Follow-up / prevention
- Keep the real desktop-bridge API v1 landing-page guardrail test always-on in CI.
- Preserve assertions that provider path resolves to distributed desktop execution, with
  non-streaming API v1 semantics.
- Maintain user-facing no-node and bridge-error messaging checks so outages fail clearly instead of
  presenting misleading stub behavior.
