# Outage: desktop-tauri operator failed to show relay registration

- **Date:** 2026-04-18
- **Slug:** `desktop-tauri-operator-relay-registration-regression`
- **Affected area:** desktop MVP operator flow (`desktop-v0.1.0`) with `relay.py` on loopback and LAN

## Summary
After clicking **Start operator** in the desktop app, the operator could remain in
`Registered: no` even while relay heartbeats were being accepted.

## Symptoms
- UI stayed at `Registered: no` while `Running: yes`.
- `Last error` could show relay incompatibility text despite relay being reachable.
- Desktop command-line output lacked step-by-step registration diagnostics.

## Impact
Operators appeared offline, making local testing and network rollout look broken.
Teams had limited visibility into whether failures came from relay connectivity,
protocol mismatch, or runtime setup.

## Root cause
1. The desktop bridge registration check treated any presence of `error` as a hard
   failure, even when relays returned heartbeat payloads with `"error": null`.
2. Bridge stderr diagnostics were too sparse around registration polling,
   request processing, and model/runtime initialization.

## Remediation
- Normalized relay error handling so `null`/empty `error` values are treated as
  non-errors while valid heartbeat responses mark the operator as registered.
- Added detailed bridge stderr instrumentation for:
  - bridge startup arguments and relay target
  - model runtime initialization start/ready/failure
  - each relay poll summary (keys, heartbeat, payload, error, wait interval)
  - request processing start/success/failure
  - bridge shutdown
- Kept existing `desktop.runtime_setup ...` output so llama-cpp runtime
  selection and fallback details are visible alongside operator lifecycle logs.

## Follow-up / prevention
- Preserve regression coverage for heartbeat responses that include nullable
  `error` fields.
- Keep critical desktop↔relay and llama runtime transitions explicitly logged
  on stderr for easy diagnosis in packaged app command windows.
