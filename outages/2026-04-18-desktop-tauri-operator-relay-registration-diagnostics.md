# Outage: desktop-tauri operator relay registration lacked actionable diagnostics

- **Date:** 2026-04-18
- **Slug:** `desktop-tauri-operator-relay-registration-diagnostics`
- **Affected area:** desktop-tauri operator bridge (`compute_node_bridge.py`) relay handshake loop

## Summary
Desktop operator sessions could sit in `Registered: no` without enough command-window detail to
pinpoint why relay registration failed. The bridge emitted minimal runtime setup logs but did not
trace critical relay registration checkpoints.

## Symptoms
- Clicking **Start operator** left the desktop UI in `Running: yes` / `Registered: no`.
- Command window logs lacked clear relay handshake checkpoints and response summaries.
- Operators could not quickly tell whether failures were caused by relay auth rejection,
  connectivity, protocol mismatch, or transient polling exceptions.

## Impact
Operator troubleshooting became slow and ambiguous. Regressions in relay registration behavior
could persist longer because users and maintainers lacked deterministic, in-band diagnostics.

## Root cause
`compute_node_bridge.py` did not emit structured logs at each critical interaction point between
Tauri and `relay.py`, and unhandled relay poll exceptions could terminate useful registration
signals prematurely. Error extraction also treated nested relay error payloads opaquely.

## Fix
- Added structured bridge stderr logs for:
  - startup arguments and resolved relay target
  - llama runtime setup and model initialization
  - each relay poll result (including response shape summary)
  - legacy relay request processing success/failure
  - shutdown lifecycle checkpoints
- Hardened polling loop to recover from unexpected exceptions and continue emitting status events.
- Improved relay error extraction so nested payloads (for example auth errors with
  `error.message`) surface as actionable status text.
- Added regression tests for nested relay error parsing and polling exception recovery.

## Follow-up / prevention
- Keep bridge logging aligned with every relay/runtime boundary transition.
- Preserve exception-recovery tests so polling failures degrade into diagnostics instead of silent
  operator stalls.
- Continue surfacing runtime backend and relay handshake metadata in status events to simplify
  field triage.
