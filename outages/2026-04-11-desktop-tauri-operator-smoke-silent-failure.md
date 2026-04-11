# Outage: desktop-v0.1.0 operator/smoke-test startup failures were effectively silent

- **Date:** 2026-04-11
- **Slug:** `desktop-tauri-operator-smoke-silent-failure`

## Summary
In `desktop-v0.1.0`, both **Start operator** and **Start local inference** could appear to do nothing when the Python sidecars failed to initialize. The desktop UI often remained in a misleading state (`starting` for inference, `running=no`/`registered=no` for operator) with little or no actionable error context.

## Impact
- Desktop MVP users could not reliably distinguish a healthy startup from a failed startup.
- Local smoke-test failures (missing interpreter/dependencies/import/runtime/model init) were hard to diagnose.
- Operator failures against incompatible/unreachable relays looked like no-ops rather than explicit failures.

## Symptoms
- Clicking **Start local inference** left status stuck at `starting` in some failure paths.
- Clicking **Start operator** could keep `Running`/`Registered` at `no` without clear reason.
- Sidecar stderr output (where Python reported real failures) was not visible in practice.

## Root cause
1. The Rust Tauri host spawned sidecar child processes with `stderr` piped but did not drain/log that stream, making startup/runtime failures easy to miss and risking blocked child progress under enough stderr output.
2. Frontend startup flow had an invoke rejection path where inference status could be stranded in `starting` without surfacing a visible error.
3. Operator relay compatibility failures were not consistently translated into actionable app-visible errors.

## Why this was silent
- Structured events were only read from child `stdout`; malformed/non-event output and `stderr` details were effectively discarded.
- Startup commands were tied to long-running process lifetimes, making button clicks feel inert when sidecars failed early.
- Some immediate invoke failures lacked explicit UI state rollback to `failed`.

## Remediation
- Drain and prefix-log sidecar/bridge `stderr` and log malformed `stdout` lines from Rust host.
- Emit explicit error events when sidecars exit non-zero or stream read fails.
- Make start commands return promptly after spawn by running sidecar lifecycles in background tasks and driving UI via emitted events.
- Add frontend error handling to ensure failed invoke transitions inference to `failed` and compute-node `last_error` is updated.
- Add relay compatibility guidance for old/unreachable relay responses (including explicit recommendation to update relay.py to repo HEAD).

## Follow-up / prevention
- Keep regression tests for stderr draining and UI failure-state transitions.
- Preserve structured, grep-friendly desktop host logs for sidecar startup/runtime failures.
- Maintain clear relay compatibility messaging in operator status so failures are actionable in first-run diagnostics.
