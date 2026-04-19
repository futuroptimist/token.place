# Outage: desktop-tauri operator startup resolved Python bridge path incorrectly on Windows

- **Date:** 2026-04-19
- **Slug:** `desktop-tauri-operator-bridge-path-regression`
- **Affected area:** desktop-tauri `Start operator` flow (`compute_node_bridge.py`) on Windows packaged installs

## Summary
After clicking **Start operator**, the desktop app briefly showed `Running: yes` and then flipped back
to `Running: no` without ever reaching `Registered: yes`.

## Symptoms
- `Running` returned to `no` roughly one relay heartbeat after startup.
- `Registered` stayed `no` and relay handshakes never completed.
- stderr showed Python launch failures like:
  - `python.exe: can't find '__main__' module in 'C:\\Users\\...\\AppData\\Local\\token.place'`

## Impact
Windows operators could not connect the packaged desktop app to a local relay, blocking operator
onboarding and any relay-backed end-to-end smoke flow from the desktop UI.

## Root cause
1. Bridge path resolution was too strict about `resources/python/compute_node_bridge.py` layouts
   and did not robustly handle flattened resource layouts used by some packaging/install contexts.
2. Startup did not fail early when the resolved Python bridge script path was missing, so failures
   surfaced later as ambiguous Python `__main__` launch errors.

## Remediation
- Expanded compute-node bridge candidate resolution to support both nested (`resources/python/...`)
  and flattened (`resources/...` and top-level) packaged resource layouts.
- Added pre-spawn validation so Python bridge launches fail immediately with a deterministic
  "script not found at resolved path" error instead of obscure interpreter-level `__main__` errors.
- Extended packaged operator e2e coverage to exercise both nested and flattened desktop resource
  layouts against a live relay.

## Follow-up / prevention
- Keep packaged operator e2e in CI and ensure it always validates bridge startup + registration
  against relay heartbeat behavior.
- Preserve path-resolution tests for future Tauri packaging layout changes.
