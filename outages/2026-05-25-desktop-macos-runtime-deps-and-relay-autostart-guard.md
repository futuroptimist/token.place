# Outage: desktop macOS bridge dependency gap and relay autostart regression guard

- **Date:** 2026-05-25
- **Slug:** `desktop-macos-runtime-deps-and-relay-autostart-guard`
- **Affected area:** desktop-tauri operator startup and desktop process lifecycle

## Summary
Packaged-like desktop launches on macOS could fail before the bridge emitted a startup event when
the resolved Python interpreter did not already have `psutil` installed. In the same window, we
also lacked explicit desktop lifecycle guard coverage proving the app never starts `relay.py`
locally (including localhost:5010).

## Symptoms
- UI showed: `compute-node bridge exited before emitting a startup event: No module named 'psutil'`.
- Operator never reached `Running: yes` in clean packaged-like launches with `PYTHONNOUSERSITE=1`.
- No dedicated desktop guard test asserted that desktop resources/startup paths exclude relay autostart.

## Root causes
1. The desktop bridge import chain could reach `utils.system.resource_monitor` before startup status,
   and `resource_monitor` imported `psutil` at module import time.
2. Packaged desktop runtime setup did not own deterministic installation/verification of a curated
   desktop runtime dependency set for the interpreter chosen by Tauri.
3. Desktop tests lacked explicit no-relay-autostart static/lifecycle checks tied to localhost:5010.

## Why tests missed it
- Existing packaged bridge coverage primarily validated import/start behavior, but did not guarantee
  dependency provisioning happened in the exact packaged-interpreter path before bridge runtime import.
- No dedicated test existed to assert desktop runtime resources/startup code excludes `relay.py`.

## Fix
- Added a curated desktop runtime dependency manifest bundled in Tauri resources:
  `desktop-tauri/src-tauri/python/requirements_desktop_runtime.txt`.
- Added deterministic desktop dependency bootstrap in `desktop_runtime_setup.py` using the selected
  interpreter before runtime probing.
- Made `utils.system.resource_monitor` resilient when `psutil` is unavailable (metrics fall back to
  safe defaults) so optional metrics imports do not crash early startup.
- Added static guard tests ensuring desktop resources/runtime code do not bundle or spawn `relay.py`.
- Added a macOS-focused lifecycle e2e guard script asserting no listener on localhost:5010 and no
  lingering `relay.py` after desktop app launch/close.

## Regression tests
- `tests/unit/test_desktop_no_relay_autostart.py`
- `desktop-tauri/scripts/test_desktop_no_relay_autostart_e2e.py`
- `desktop-tauri/scripts/test_packaged_operator_e2e.py` (packaged layout now includes curated runtime requirements manifest)
