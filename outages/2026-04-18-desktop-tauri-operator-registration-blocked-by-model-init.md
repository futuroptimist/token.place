# Outage: desktop-tauri operator registration was blocked by model initialization

- **Date:** 2026-04-18
- **Severity:** medium
- **Status:** resolved

## Summary
Desktop Tauri operator startup attempted to fully initialize `llama_cpp_python` before the first
relay `/sink` poll. On slower model loads, backend fallback paths, or failed runtime initialization,
the compute node never reached the relay registration loop in time, so UI state remained
"Running: yes" and "Registered: no" even when `relay.py` was healthy on loopback/network targets.

## Symptoms
- Clicking **Start operator** left registration stuck at **no**.
- Relay health checks were green, but no successful operator registration was observed.
- Desktop command-line output lacked enough phase-level diagnostics to pinpoint where startup was
  blocked.

## Impact
Desktop operators could appear online locally while never participating in relay request routing,
which prevented inference dispatch through the operator path.

## Root cause
The bridge startup sequence in `compute_node_bridge.py` performed `runtime.ensure_model_ready()` as
a hard gate before any relay registration/poll work. This coupled relay liveness to model runtime
initialization and made relay registration contingent on local model readiness.

## Fix
- Moved model readiness to a lazy path inside the polling loop so relay registration can happen
  immediately.
- Added phase-level stderr diagnostics for startup, relay targeting, poll begin/result/sleep, model
  initialization begin/success/failure, and request-processing begin/success/failure/skip.
- Extended status payloads with `model_runtime_ready` so UI/diagnostics can distinguish "registered
  but model not ready" from transport failures.

## Follow-up / prevention
- Keep relay registration and model readiness as independently observable states.
- Ensure desktop bridge startup logs include every critical hand-off: runtime setup, relay
  registration attempt, and inference processing lifecycle.
- Maintain regression tests that verify registration can succeed even when model initialization fails.
