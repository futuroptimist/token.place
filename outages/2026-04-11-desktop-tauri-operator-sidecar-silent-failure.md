# Outage: desktop-tauri operator and local smoke test silent startup failures

- **Date:** 2026-04-11
- **Slug:** `desktop-tauri-operator-sidecar-silent-failure`
- **Affected surface:** desktop-tauri `desktop-v0.1.0`

## Symptoms
- Clicking **Start operator** appeared to do nothing.
- **Running** and **Registered** stayed `no` with no actionable explanation.
- Clicking **Start local inference** could leave status at `starting` with no visible reason.
- Python-side failures were hard to diagnose in packaged desktop runs.

## Impact
Desktop MVP smoke tests and operator bring-up were unreliable to operate and difficult to
troubleshoot. Users could not quickly distinguish a healthy startup from bridge/runtime failures.

## Root cause
The Tauri Rust host launched Python child processes with `stderr` piped but did not actively
drain and surface those logs. Startup/runtime failures (missing interpreter/dependencies, import
errors, model init issues, relay registration failures) were therefore easy to miss, and could also
risk pipe backpressure stalls. In parallel, frontend startup invoke handling had a silent-failure
path (`startInference()` lacked `try/catch`) that could strand status at `starting`.

## Why it was silent
- Child `stderr` output from `inference_sidecar.py` and `compute_node_bridge.py` was not emitted to
  shell logs.
- Malformed/non-JSON stdout lines were ignored without context.
- UI error state on invoke rejection was incomplete for local inference startup.
- Operator compatibility errors from old relay protocol responses were not explicit.

## Remediation
- Added stderr draining for inference sidecar and compute-node bridge with structured,
  grep-friendly Rust log prefixes.
- Logged malformed stdout lines with parse context.
- Emitted explicit UI-visible startup failure events on early child exit and start failures.
- Updated frontend startup flows so invoke rejection transitions to `failed` / `last_error` instead
  of appearing as a no-op.
- Added explicit operator compatibility messaging for older/incompatible relay payloads.
- Added regression tests across Rust/Python/frontend for this failure mode.

## Follow-up / prevention
- Keep event-driven startup UX with immediate command return and lifecycle updates via events.
- Preserve stderr drain + structured prefix logging in all future sidecars/bridges.
- Keep protocol compatibility checks explicit so outdated relays fail loudly and actionably.
