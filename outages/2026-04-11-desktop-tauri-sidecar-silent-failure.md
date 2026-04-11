# Outage: desktop-tauri sidecar startup failures were silent

- **Date:** 2026-04-11
- **Slug:** `desktop-tauri-sidecar-silent-failure`
- **Affected area:** desktop MVP (`desktop-v0.1.0`) local inference + operator start flows

## Summary
In desktop `v0.1.0`, users could click **Start local inference** or **Start operator** and see no
useful feedback: the UI could remain in `starting`, `Running`/`Registered` could stay `no`, and
the root startup error was often invisible.

## Symptoms
- Start local inference appeared to do nothing.
- Start operator appeared to do nothing.
- Local inference could remain stuck in `starting` after invoke rejection.
- Operator `Last error` was often empty even when startup failed.

## Impact
Desktop smoke tests and operator bring-up were hard to diagnose. Failures such as missing Python,
import/runtime issues, relay incompatibility, and bridge startup crashes looked like no-ops.

## Root cause
1. Rust host side spawned Python children with `stderr` piped but did not drain/report it.
2. Rust host side ignored malformed stdout lines without logging context.
3. Frontend start actions did not consistently catch invoke failures, so state could remain in an
   in-between `starting` state.
4. Compute-node bridge treated non-legacy relay payloads as effectively successful status updates,
   instead of surfacing a compatibility error for older/incompatible relays.

## Why it was silent
- The most actionable errors were printed to child `stderr`, but the parent process did not read
  and surface those lines.
- Invoke rejection paths were not always mapped to failed UI state.
- Relay compatibility mismatch did not produce a clear user-facing message.

## Remediation
- Drain and prefix-log child `stderr` for both inference sidecar and compute-node bridge.
- Prefix-log malformed stdout lines for both sidecar paths.
- Emit fallback error events when child exits non-zero without structured error event payloads.
- Make desktop start commands return promptly after spawn and push lifecycle/status via events.
- Handle frontend invoke failures by setting failed/error state.
- Emit actionable relay compatibility error:
  `relay response is incompatible with desktop-v0.1.0 operator; upgrade relay.py to HEAD`.

## Follow-up / prevention
- Keep startup lifecycle event-driven and include explicit startup-failure events.
- Keep stderr draining in place for all future desktop child-process integrations.
- Ensure child-exit fallback errors do not overwrite more actionable structured errors already emitted
  on stdout.
- Preserve regression coverage for:
  - malformed stdout line handling (ignore/log malformed lines while keeping valid event flow)
  - sidecar/bridge stderr drain behavior
  - inference start invoke rejection UI behavior
  - operator relay incompatibility error surfacing
