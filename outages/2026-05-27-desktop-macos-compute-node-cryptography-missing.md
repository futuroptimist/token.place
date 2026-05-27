# 2026-05-27 — macOS desktop compute-node bridge failed before startup event on missing `cryptography`

## Summary
The macOS Tauri desktop app failed to start the compute node operator in release-like environments because `compute_node_bridge.py` attempted to import runtime modules before dependency preflight completed, surfacing `No module named 'cryptography'` and exiting before a startup event.

## User impact
Users clicking **Start operator** saw:
`Last error: compute-node bridge exited before emitting a startup event: No module named 'cryptography'`.
Operator startup never reached stable `started`/`status` events.

## Root cause
`compute_node_bridge.main()` imported `normalize_compute_mode` from `utils.compute_node_runtime` before `run()` executed desktop runtime dependency bootstrap. In clean packaged Python environments (`PYTHONNOUSERSITE=1`, clean HOME), that early import chain reached modules requiring `cryptography` before `ensure_desktop_python_dependencies()` had a chance to install/validate desktop runtime prerequisites.

## Fix
- Moved compute mode normalization off the preflight path in `main()` and into `run()` after runtime preflight/import setup.
- Kept startup failure handling structured (`type=error`) so bridge exits no longer fail silently before first JSON event.
- Extended packaged e2e coverage to include a release-like macOS `.app/Contents/Resources` layout, preventing synthetic layout-only regressions.

## Verification
- Unit bridge tests validate run-path mode normalization and structured startup failure semantics.
- Packaged e2e now probes compute bridge import behavior under both `resources/` and macOS `Contents/Resources` layouts in isolated env (`PYTHONNOUSERSITE=1`, temp HOME).

## Prevention
- Preserve dependency preflight-before-runtime-import ordering for desktop bridge entrypoints.
- Keep macOS `.app` layout checks in packaged e2e to mirror real bundle resource resolution.
