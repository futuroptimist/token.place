# Outage: desktop macOS compute-node bridge cryptography missing before startup event

- **Date:** 2026-05-27
- **Slug:** `desktop-macos-compute-node-cryptography-missing`
- **Affected area:** desktop-tauri compute-node operator startup in packaged macOS app bundles

## Summary
On macOS desktop release bundles, starting the compute-node operator could fail with `compute-node bridge exited before emitting a startup event: No module named 'cryptography'`.
The bridge terminated too early in startup ordering, preventing a normal startup/status/error event sequence.

## Impact
- Users could not start the compute node operator from the desktop UI in clean packaged environments.
- The UI surfaced an early-exit startup message instead of structured dependency diagnostics.

## Root cause
- `compute_node_bridge.main()` imported `utils.compute_node_runtime.normalize_compute_mode` before dependency preflight.
- That import chain reached modules that require `cryptography`, so `ModuleNotFoundError` occurred before `ensure_desktop_python_dependencies()` ran.
- Desktop requirements discovery also omitted macOS `Contents/Resources/python/requirements_desktop_runtime.txt` as an explicit candidate path.

## Fix
- Moved compute mode normalization into `run()` after dependency preflight and after runtime module imports are guarded.
- Kept early failure handling eventized so startup paths emit structured `error` events rather than raw crashes.
- Added macOS `Contents/Resources/python/requirements_desktop_runtime.txt` path resolution coverage in runtime setup.
- Added targeted regression tests to lock startup ordering and macOS requirements path behavior.

## Verification
- Unit tests for compute-node bridge startup ordering and dependency preflight behavior.
- Unit tests for desktop runtime requirements discovery in macOS bundle layout.
- Packaged operator e2e checks with `PYTHONNOUSERSITE=1` and clean temp HOME continue to validate bridge behavior.

## Prevention
- Keep bridge imports dependency-safe before desktop dependency preflight.
- Maintain explicit tests for real/release-like macOS bundle layout (`.app/Contents/Resources`).
- Continue asserting that early startup failures are emitted as structured events.
