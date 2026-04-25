# Outage: relay landing-page desktop-bridge/API v1 mismatch

- **Date:** 2026-04-24
- **Slug:** `relay-landing-page-desktop-bridge-api-v1-mismatch`
- **Affected area:** relay-served landing-page chat (`/`) routed through API v1 and desktop bridge

## Summary
The landing-page UI sent requests to `/api/v1/chat/completions`, but the desktop bridge only
participated in legacy relay polling/response handling. This made operator status look healthy while
real landing-page API v1 requests were not actually serviced by the registered desktop node.

## Impact / user-visible symptoms
- Desktop operator could show healthy registration/heartbeat while landing-page requests were not
  handled by the bridge.
- Users could receive a fake/stub assistant response instead of real desktop inference.
- Failure messaging remained weak when no relay compute nodes were available.

## Root cause
1. Protocol-path mismatch: landing-page traffic used API v1 while desktop bridge request handling
   still accepted legacy/non-API-v1 relay payload paths.
2. Registration health and request-processing health were treated as equivalent, so heartbeat success
   masked request-path incompatibility.

## Contributing factors
- Existing smoke coverage allowed mocked `/api/v1/chat/completions` flows that validated rendering
  but not relay queue/sink/source behavior through `compute_node_bridge.py`.
- Guardrail setup allowed provider-path outcomes that could pass while effectively validating local
  provider bypass instead of enforced distributed desktop-bridge execution.

## Why CI/tests missed it
CI validated the old guardrail surface, which proved the local-provider bypass path could succeed
without proving that a registered desktop bridge processed API v1, non-streaming relay payloads.
As a result, the test signal showed green while real landing-page bridge servicing was still broken.

## Resolution
Following the preceding fixes, guardrails now enforce and verify the intended final path:
- API v1 distributed routing is forced for the real landing-page bridge e2e fixture in
  `tests/conftest.py` (`TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED=1`, distributed fallback off).
- The unmocked e2e in `tests/e2e/test_ui.py::test_landing_chat_real_inference_with_desktop_bridge_api_v1`
  now requires:
  - API v1 only (no API v2),
  - non-streaming responses,
  - distributed provider + `registered_desktop_compute_node` execution backend,
  - desktop-bridge `process_request` evidence for API v1 payload handling,
  - non-stub assistant output when runtime reports real inference support.
- CI (`.github/workflows/ci.yml`) now runs this guardrail with a tiny real GGUF model before the
  full suite and keeps the same model path during `./run_all_tests.sh`.

## Preventive follow-ups / guardrails added
- Keep the always-on CI relay landing-page desktop-bridge API v1 guardrail workflow step mandatory.
- Preserve assertions that fail if bridge processing falls back to legacy/non-API-v1 or streaming
  relay request handling.
- Continue requiring explicit distributed-provider diagnostics in landing-page e2e headers so
  local-provider bypass cannot silently satisfy the guardrail.
