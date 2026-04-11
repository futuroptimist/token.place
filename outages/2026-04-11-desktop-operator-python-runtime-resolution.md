# Outage: desktop operator start failed when Python runtime could not be resolved on Windows

- **Date:** 2026-04-11
- **Slug:** `desktop-operator-python-runtime-resolution`
- **Affected area:** desktop-tauri compute-node operator startup (`Start operator`)

## Summary
On Windows desktop environments where `python3` is not available (or where only the Microsoft Store
execution alias exists), clicking **Start operator** could briefly show `Running: yes` and then
immediately return to `Running: no`.

The command window displayed `Python was not found`, and the bridge process exited before
registration.

## Impact
- Operator startup was unreliable on affected Windows hosts.
- Users received a transient success state followed by failure.
- The failure message in app status was less actionable than it could be.

## Root cause
The desktop compute-node and sidecar launch paths were hardcoded to prefer a Python launcher that
was not robust across platforms and installation patterns. In affected Windows setups, the selected
launcher was unavailable/invalid, so the Python bridge terminated at startup.

## Resolution
Added a shared Python runtime resolver for desktop-tauri:

1. Honors explicit `TOKEN_PLACE_SIDECAR_PYTHON` override.
2. Validates candidate launchers using `--version` before use.
3. Uses platform-aware fallback order (`py -3`, `python`, `python3` on Windows; `python3`,
   `python` elsewhere).
4. Fails fast with explicit in-app error when no usable interpreter is found, rather than spawning a
   broken process.

Applied this resolver to both:
- compute-node bridge startup (`Start operator` flow), and
- local inference sidecar startup.

## Preventive actions
- Keep Python runtime selection centralized for all desktop Python child process launches.
- Preserve fail-fast checks with explicit errors before spawning bridge/sidecar scripts.
- Maintain unit coverage for interpreter resolution and error reporting behavior.
