# Outage: desktop macOS compute-node bridge missing cryptography preflight

- **Date:** 2026-05-27
- **Slug:** `desktop-macos-compute-node-cryptography-missing`
- **Affected area:** desktop-tauri compute-node startup in packaged macOS layout

## Summary
Desktop macOS packaged startup could fail with `compute-node bridge exited before emitting a startup event: No module named 'cryptography'`.
The failure happened before the bridge emitted its first startup/status event, so the UI only surfaced a bridge-exited error.

## Impact
On clean packaged-like environments (`PYTHONNOUSERSITE=1`, empty HOME), starting the operator from desktop UI could fail immediately, blocking local compute-node bring-up.

## Root cause
- `compute_node_bridge.py` imported `utils.compute_node_runtime` in `main()` just to normalize mode **before** runtime dependency preflight.
- That early import reached crypto-dependent modules prior to `ensure_desktop_python_dependencies()`, so missing `cryptography` raised `ModuleNotFoundError` too early.
- Existing packaged tests covered `resources/python` layout but not a release-like macOS `.app/Contents/Resources` path in the same dependency preflight + bridge startup flow.

## Fix
- Moved runtime path bootstrap (`ensure_runtime_import_paths`) to execute before importing desktop runtime setup helpers.
- Removed early compute-runtime import from `main()` and replaced it with a local mode normalizer so dependency preflight executes first inside `run()`.
- Extended packaged operator e2e to validate both standard `resources/` and macOS `.app/Contents/Resources` layouts with dependency preflight and bridge import probes under isolated env (`PYTHONNOUSERSITE=1`, clean HOME).
- Added unit coverage for macOS requirements file discovery path and startup ordering guard (no early compute-runtime import in `main()`).

## Verification
- Targeted desktop unit suites pass for runtime setup, compute bridge, and model bridge.
- Packaged operator e2e inspect path now validates macOS release-like layout and rejects `No module named`, `ModuleNotFoundError`, and `ImportError` regressions.

## Prevention
Keep compute-node bridge startup contract explicit: path bootstrap and dependency preflight must run before imports that can transitively require `cryptography`, and packaged e2e must include real macOS bundle path shapes.
