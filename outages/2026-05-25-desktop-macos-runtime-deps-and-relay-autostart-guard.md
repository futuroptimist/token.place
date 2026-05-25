# Outage: desktop macOS operator startup dependency gap + relay autostart guardrail gap

- **Date:** 2026-05-25
- **Slug:** `desktop-macos-runtime-deps-and-relay-autostart-guard`
- **Affected area:** desktop-tauri operator startup (`desktop-v0.1.0`) on packaged-like macOS launches

## Summary
`Start operator` could fail before the bridge emitted a startup event when packaged desktop Python runtimes were missing dependencies (notably `psutil`) under `PYTHONNOUSERSITE=1`. In parallel, desktop regression coverage was missing an explicit guard that desktop resources and startup code never introduce `relay.py` autostart/`localhost:5010` ownership.

## Symptoms
- UI error included: `compute-node bridge exited before emitting a startup event: No module named 'psutil'`.
- Operator did not transition to `Running: yes`.
- No explicit static/lifecycle guard was in place to prevent future desktop autostart paths for `relay.py` and localhost `5010`.

## Root causes
1. `utils/system/resource_monitor.py` imported `psutil` at module-import time, so any import chain touching resource monitoring could crash bridge startup before status events.
2. Packaged-like launch conditions (`PYTHONNOUSERSITE=1`) exposed missing interpreter-level runtime deps that local developer environments masked.
3. Desktop tests lacked a dedicated no-relay-autostart guard for bundle resource contracts and desktop startup source paths.

## Why tests missed it
- Existing packaged-bridge checks were primarily focused on runtime path wiring and bridge behavior, but not enough static guardrails around forbidden desktop relay ownership paths.
- Developer-local Python environments often already had `psutil` installed globally, masking packaged interpreter dependency gaps.

## Fix
- Made resource monitoring resilient when `psutil` is not importable by falling back to safe zeroed CPU/memory metrics instead of crashing import-time.
- Added static desktop guard tests that fail if desktop bundle resources include relay artifacts or if core desktop startup/runtime files reference `relay.py` or `localhost:5010`.
- Strengthened existing Tauri bundle resource unit coverage with an explicit forbidden relay resource assertion.

## Tests
- `python -m pytest tests/unit/test_resource_monitor.py tests/unit/test_desktop_no_relay_autostart.py`
- `cargo test --manifest-path desktop-tauri/src-tauri/Cargo.toml tauri_bundle_resources_include_python_bridge_scripts`
