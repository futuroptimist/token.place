# 2026-05-24 — macOS desktop operator startup failed on missing `requests`

## Summary
macOS desktop operator startup failed before readiness when the packaged compute-node bridge imported Python modules that required `requests`, but the packaged/runtime interpreter did not include that dependency.

## User impact
Users clicking **Start operator** saw startup fail with `Last error: compute-node bridge exited before emitting a startup event: No module named 'requests'`, and operator state remained unavailable.

## Symptoms
- Startup exited before `started`/stable registration lifecycle completed.
- UI showed actionable startup error text in the existing **Last error** region.

## Root cause
`utils/networking/relay_client.py` and `utils/llm/model_manager.py` imported `requests` at module import time. Desktop bridge startup imports those modules through the compute runtime path, so startup depended on an undeclared third-party module in packaged/clean Python environments.

## Why tests missed it
Coverage focused on bridge event handling and startup surfacing, but not on import-time dependency resilience when `requests` is absent from the runtime used by desktop startup.

## Fix implemented
- Added a deterministic stdlib-backed HTTP compatibility layer (`utils/networking/http_requests_compat.py`) that avoids third-party `requests` entirely for bridge/runtime startup surfaces and uses a small stdlib (`urllib`) implementation.
- Switched bridge runtime dependency import sites (`relay_client.py`, `model_manager.py`) to use the compatibility layer, removing hard import-time failure on `requests`.
- Added startup-path regression coverage that blocks `requests` imports and verifies importing `utils.networking.relay_client` and `utils.llm.model_manager` still succeeds.

## Tests added/repaired
- `tests/unit/test_http_requests_compat.py` validates fallback import path without `requests`.
- Existing bridge startup regression suites continue validating startup/error semantics.

## Follow-up items
- Expand desktop e2e CI assertions to explicitly run startup with `requests` absent from site-packages on macOS runners when available.
