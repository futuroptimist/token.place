# Outage: desktop-tauri operator startup failed on Windows Python alias

- **Date:** 2026-04-11
- **Slug:** `desktop-tauri-python-alias-runtime-failure`
- **Affected area:** desktop compute-node operator startup (`desktop-v0.1.0`)

## Summary
On Windows workstations without a real Python interpreter on PATH, clicking
**Start operator** in the desktop app briefly showed `Running: yes`, then flipped back to
`Running: no` with bridge exit code `9009`. The command window showed
`Python was not found`.

## Symptoms
- `Start operator` appears to start and then immediately stops.
- `Last error` reports bridge non-zero exit status.
- Console shows Microsoft Store alias text:
  `Python was not found; run without arguments to install from the Microsoft Store...`

## Impact
Desktop operators could not come online on affected Windows hosts, blocking compute-node
registration and local operator workflows.

## Root cause
Python subprocess launching used hardcoded executable names (`python`/`python3`) and
assumed PATH contained a working interpreter. On Windows, `python.exe` can resolve to an
App Execution Alias shim that launches the Store prompt and exits with code `9009`.
The desktop host treated spawn as successful (process started), then the bridge exited
immediately.

## Remediation
- Added a shared Python runtime resolver in Tauri Rust code that probes candidates with a
  lightweight execution check before use.
- Resolver now prefers:
  - explicit env override (`TOKEN_PLACE_PYTHON` / `TOKEN_PLACE_SIDECAR_PYTHON`) when valid,
  - then platform-aware fallbacks (`py -3`, `python`, `python3` on Windows; `python3`,
    `python`, `py -3` elsewhere).
- Applied resolver to all desktop Python launch paths:
  - compute-node bridge
  - inference sidecar
  - model bridge helper
- Added unit coverage for explicit valid runtime and invalid override fallback behavior.

## Follow-up / prevention
- Keep all desktop Python entrypoints routed through one resolver to avoid drift.
- Continue probing interpreter executables before long-running bridge startup.
- Preserve operator smoke tests on Windows hosts where App Execution Aliases are enabled.
