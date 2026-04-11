# Outage: desktop-tauri operator start failed on Windows without a usable Python launcher

- **Date:** 2026-04-11
- **Slug:** `desktop-tauri-operator-python-launcher-windows`
- **Affected area:** desktop-tauri `Start operator` flow (`compute_node_bridge.py`)

## Summary
On Windows hosts where `python` resolves to the Microsoft Store execution alias (or where
`python3` is not installed), clicking **Start operator** briefly set `Running: yes` and then
immediately reverted to `Running: no`.

## Symptoms
- `Running` flips to `yes` and immediately back to `no`.
- `Registered` stays `no`.
- Desktop log window shows:
  `Python was not found; run without arguments to install from the Microsoft Store ...`
- UI `Last error` reports bridge exit status (`exit code: 9009` on affected Windows systems).

## Impact
Operator onboarding was blocked for affected Windows users even when the rest of the desktop app
was functional.

## Root cause
The Rust host launched Python bridge scripts using a single hard-coded launcher expectation
(`python3` default or an env override) and did not proactively validate interpreter usability.
On affected Windows setups, launcher resolution selected an unusable command path that exited
immediately.

## Remediation
- Added shared Python launcher resolution with platform-aware fallback candidates.
  - Windows order: `py -3`, `python`, `python3`.
  - Non-Windows order: `python3`, `python`.
- Added active launcher validation (`--version`) before spawning bridge scripts.
- Added explicit detection of the Windows Store alias failure string and automatic fallback to the
  next candidate.
- Wired both operator bridge and local inference sidecar startup through the same robust launcher
  resolver.

## Follow-up / prevention
- Keep a single launcher-resolution path for all desktop Python subprocesses.
- Continue surfacing concrete startup diagnostics in UI and stderr logs.
- Preserve regression tests around launcher failure signatures and fallback behavior.
