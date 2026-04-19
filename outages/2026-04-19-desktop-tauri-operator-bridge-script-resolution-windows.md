# Outage: desktop-tauri operator bridge script resolution failed on Windows packaged installs

- **Date:** 2026-04-19
- **Slug:** `desktop-tauri-operator-bridge-script-resolution-windows`
- **Affected area:** desktop-tauri operator flow (`Start operator`) on Windows packaged app installs

## Summary
After clicking **Start operator**, the desktop app could briefly transition to running and then
fall back to `Running: no` while never reaching `Registered: yes`.

## Symptoms
- UI flipped from `Running: yes` back to `Running: no` shortly after startup.
- `Registered: yes` never appeared.
- stderr logs included Python startup failures like:
  - `python.exe: can't find '__main__' module in '...\\token.place'`

## Impact
Operators could not complete desktop→relay registration on Windows packaged installs,
blocking local relay validation and operator-assisted inference flows.

## Root cause
1. Compute-node bridge script discovery did not prioritize runtime resource directory paths exposed
   by Tauri at runtime.
2. Candidate coverage for Windows updater layouts was incomplete, increasing the chance that
   packaged bridge script resolution would miss the real `compute_node_bridge.py` location.
3. Existing packaged e2e only asserted startup, not successful relay registration.

## Remediation
- Added runtime `resource_dir` bridge candidates in compute-node script resolution.
- Added Windows updater-style resource candidate paths (`_up_/resources/python/...`).
- Expanded regression coverage:
  - Rust unit test asserts runtime resource and updater candidates are included.
  - Packaged operator e2e now requires `registered=true` before shutdown.
  - CI now runs packaged operator e2e on `windows-latest`.

## Follow-up / prevention
- Keep Windows packaged operator e2e mandatory for desktop-tauri bridge/path changes.
- Preserve registration-state assertions (`registered=true`) in relay-connected e2e tests.
- Continue logging bridge stderr at startup so path-resolution failures remain diagnosable.
