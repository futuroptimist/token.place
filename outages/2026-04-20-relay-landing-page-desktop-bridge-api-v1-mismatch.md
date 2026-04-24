# Outage: relay landing-page desktop-bridge/API v1 mismatch

- **Date:** 2026-04-20
- **Slug:** `relay-landing-page-desktop-bridge-api-v1-mismatch`
- **Affected area:** relay landing-page chat (`/`) routed through `/api/v1/chat/completions`

## Summary
The relay landing-page chat called `/api/v1/chat/completions`, but the desktop bridge process was
only participating in the legacy relay polling/response contract at the time. This created a
health-signaling gap where the desktop app could look registered while landing-page requests did
not execute real desktop inference.

## User-visible symptoms
- Landing-page chat returned a fake/stub assistant reply instead of real desktop-generated output.
- When no compute nodes were available, user messaging was generic and did not clearly explain the
  unavailable-node condition.
- Desktop UI/operator status could appear healthy (`Running`/registered) while landing-page chat
  still failed to use the real bridge path.

## Impact
Users could not rely on the landing page to validate real desktop inference behavior, even when
operator status suggested the bridge was healthy. This undermined relay-path confidence and masked
real availability failures behind non-actionable UI responses.

## Root cause
1. Protocol/contract mismatch: the landing page used API v1 chat-completions semantics while the
   bridge integration path that CI effectively exercised still tolerated a local-provider bypass and
   did not prove the true desktop relay handoff.
2. The registration signal was necessary but not sufficient: bridge registration/polling success was
   treated as an end-to-end readiness indicator even though landing-page API v1 request servicing
   was not actually guarded.

## Contributing factors
- Earlier browser tests mocked `/api/v1/chat/completions`, so endpoint selection and UI rendering
  could pass without real relay+bridge inference.
- Guardrail scope was too broad/misaligned, allowing checks to pass while validating the wrong
  execution path.

## Why CI/tests missed it
CI coverage validated a path that could succeed via local-provider behavior rather than strictly
proving `browser -> relay API v1 -> desktop bridge runtime -> relay response -> browser`. As a
result, the tests gave false confidence that landing-page API v1 traffic was backed by real bridge
execution.

## Resolution (final state after fixes landed)
- Landing-page behavior was locked to API v1, non-streaming guardrails and stricter UI assertions.
- Added and hardened real desktop-bridge landing-page guardrail coverage in
  `tests/e2e/test_ui.py`, `tests/unit/test_api_v1_routes_additional.py`, and
  `tests/unit/test_touch_ui.py`.
- CI and local orchestration were updated so the guardrail runs against a tiny real GGUF model via
  `.github/workflows/ci.yml` and `run_all_tests.sh`, preventing stub-only pass conditions.
- Landing-page error handling was tightened so unavailable-node and bridge timeout/failure states
  map to explicit user-facing messages.

## Preventive follow-ups / guardrails
- Keep at least one always-on unmocked landing-page guardrail that requires real desktop bridge
  inference over API v1.
- Continue enforcing API v1-only, non-streaming relay landing-page behavior for `v0.1.0` paths.
- Preserve structured error mapping checks so node-unavailable and bridge-failure states remain
  actionable in the UI.
