# Outage: relay landing-page desktop-bridge/API v1 mismatch

- **Date:** 2026-04-20
- **Slug:** `relay-landing-page-desktop-bridge-api-v1-mismatch`
- **Affected area:** relay-served landing-page chat (`/`) with desktop operator connected

## Summary
The landing-page chat path used `POST /api/v1/chat/completions`, but the desktop bridge was only
participating in legacy relay polling semantics for request handling. That mismatch let the
desktop app appear healthy (`Running`/`Registered`) while not actually servicing landing-page API v1
requests end-to-end.

## Symptoms
- Users could see desktop operator status as healthy while landing-page requests were not processed
  by the registered desktop bridge.
- Failure messaging was weak for degraded relay availability, including poor UX when no compute
  nodes were available.

## Impact
- Landing-page responses could fall back to a fake/stub-style result instead of real desktop
  inference in scenarios where real-provider path assumptions were bypassed.

## Root cause
1. Contract drift across boundaries: landing-page traffic was pinned to API v1, while bridge-side
   execution validation was effectively aligned with legacy relay behavior rather than strict API v1
   request servicing.
2. Health/registration signals were treated as sufficient evidence of readiness, even though they did
   not prove that the bridge processed real landing-page API v1 requests.

## Contributing factors
- Existing UI smoke coverage relied on mocked `/api/v1/chat/completions`, which validated endpoint
  selection/rendering but not the real relay queue + bridge execution path.
- Guardrails focused on startup and registration success before adding assertions for bridge request
  processing semantics (`api_v1_payload`, non-streaming, and provider-path headers).

## Why CI/tests missed it
CI initially validated a local-provider/mocked-path bypass instead of enforcing the full browser →
relay API v1 → relay sink/source → desktop bridge contract. As a result, checks could pass
while the desktop bridge never handled the landing-page request payload that users depend on.

## Remediation
- Added an unmocked e2e guardrail in `tests/e2e/test_ui.py::test_landing_chat_real_inference_with_desktop_bridge_api_v1`
  that requires:
  - API v1 route usage and non-streaming behavior,
  - distributed provider/header diagnostics for registered desktop execution,
  - desktop bridge processing evidence in stderr (`process_request.start/ok` with `api_v1_payload=True`),
  - and non-stub assistant output when runtime reports real inference support.
- Updated CI (`.github/workflows/ci.yml`) to run this guardrail explicitly with a tiny real GGUF
  model and keep it always-on via `TOKENPLACE_REAL_E2E_MODEL_PATH`.
- Tightened landing-page failure handling so no-node and bridge-error states surface actionable user
  messages instead of generic failures.

## Follow-up / prevention
- Keep relay landing-page desktop-bridge API v1 guardrail mandatory in CI.
- Keep assertions that reject v2/streaming drift on relay landing-page traffic.
- Preserve user-facing error-code mapping coverage for no-node and bridge-failure conditions.
