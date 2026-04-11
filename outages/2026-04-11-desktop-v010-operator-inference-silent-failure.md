# Outage: desktop-v0.1.0 operator/inference startup failures were silent

- **Date:** 2026-04-11
- **Slug:** `desktop-v010-operator-inference-silent-failure`
- **Affected component:** `desktop-tauri` MVP (local smoke test + operator start)

## Summary
Desktop MVP startup failures for local inference and compute-node operator were easy to miss in practice: the UI could remain in `starting`, registration stayed `no`, and actionable runtime errors were not visible to operators.

## Impact
- Users could click **Start local inference** or **Start operator** and observe apparent no-op behavior.
- Local smoke tests failed to provide actionable diagnostics for runtime/import/model init failures.
- Operator startup issues (relay unreachable or incompatible legacy relay contract) could look like stalled registration.

## Symptoms
- `Start local inference` looked stuck with no obvious reason.
- `Start operator` looked stuck; `Running`/`Registered` did not progress meaningfully.
- Errors from Python sidecars/bridge were effectively invisible during packaged desktop runs.

## Root cause
1. Rust host spawned Python child processes with `stderr` piped but did not drain/log those streams, hiding bridge/sidecar failures and risking blocked child writes under error-heavy conditions.
2. Rust stdout parsers ignored malformed lines silently, reducing diagnosability when sidecar/bridge output was unexpected.
3. Frontend start handlers relied on awaited invoke calls; rejected invokes were not consistently surfaced into stable failed UI state (`startInference` lacked catch handling).

## Why this was silent
The user-facing state machine depended mostly on emitted structured events, but failure channels outside those events (spawn errors, stderr-only exceptions, malformed output) were dropped or hard to observe, creating a no-op UX.

## Remediation
- Added stderr draining/logging for inference sidecar and compute-node bridge with grep-friendly prefixes in Rust host logs.
- Added malformed stdout line logging for both flows.
- Added explicit early-exit inference error emission when sidecar exits non-zero before terminal event.
- Added frontend invoke rejection handling so start failures transition to visible failed state and update `Last error`.
- Added relay error classification in compute-node bridge to include actionable hints for unreachable or incompatible relay implementations (including older `relay.py` revisions like `aef3057bc4f4c895c96a1ba9e90dd0434baf3452`).

## Follow-up / prevention
- Keep stderr draining as a non-negotiable contract for desktop child-process launches.
- Keep UI startup paths event-driven but fail-fast on invoke rejection.
- Preserve and extend regression tests for stderr draining and failed-start UX transitions.
- Include relay compatibility notes in operator diagnostics and release notes for desktop MVP iterations.
