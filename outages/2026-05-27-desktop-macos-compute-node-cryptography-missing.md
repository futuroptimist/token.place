# 2026-05-27 — macOS desktop compute-node startup failed on missing `cryptography`

## Summary
On macOS desktop release bundles, starting the compute-node operator could fail before the bridge emitted its first startup event, surfacing: `compute-node bridge exited before emitting a startup event: No module named 'cryptography'`.

## User impact
Users clicking **Start operator** in the desktop app could not bring the operator online. The bridge process exited too early, and operator startup never reached normal `started` / `status` lifecycle events.

## Root cause
The compute-node startup path could fail during runtime import/bootstrap error handling with a raw `ModuleNotFoundError`, and existing packaged e2e checks did not explicitly block the `cryptography`-missing failure signature on the compute-node path.

## Fix implemented
- Moved runtime path bootstrap to run before desktop runtime helper imports in `compute_node_bridge.py`, improving packaged release import-root setup ordering.
- Hardened `main()` error handling to emit a structured bridge error event for `ModuleNotFoundError` cases (including missing `cryptography`) instead of a silent/unstyled early crash shape.
- Extended packaged operator e2e checks to fail on:
  - `No module named 'cryptography'`
  - `compute-node bridge exited before emitting a startup event`
  - `ModuleNotFoundError`
  - `ImportError`
- Added targeted unit coverage for the structured dependency-missing startup error path.

## Verification
- Unit suites for desktop runtime setup, compute-node bridge, model bridge, packaged layout, and PEP 604 runtime alias guards.
- Packaged operator inspect-only and full packaged operator e2e checks both pass in a clean `PYTHONNOUSERSITE=1` style environment.

## Prevention
- Keep compute-node bridge import/bootstrap ordering explicit and early for packaged macOS layouts.
- Preserve regression checks that reject raw missing-dependency startup failures before first bridge event.
