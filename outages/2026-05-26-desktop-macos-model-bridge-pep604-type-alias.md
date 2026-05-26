# Outage: desktop macOS model bridge failed on runtime-evaluated PEP 604 type alias

- **Date:** 2026-05-26
- **Slug:** `desktop-macos-model-bridge-pep604-type-alias`
- **Affected area:** desktop-tauri operator startup / model bridge inspect on macOS Python 3.9 environments

## Summary
Desktop operator startup could fail with `Model bridge failure: unsupported operand type(s) for |: 'type' and 'type'` before inference began.

## Root cause
- `utils/system/resource_monitor.py` declared `GpuMetrics = Dict[str, float | int | bool]` as a runtime assignment.
- On Python 3.9, `type | type` is invalid outside postponed annotations, so import-time evaluation raised `TypeError`.
- `model_bridge.py` imports `utils.llm.model_manager`, which imports `utils.system.resource_monitor`; the exception was surfaced through the model bridge catch-all error path.

## Why tests missed it
- Existing packaged bridge checks focused on dependency presence and happy-path inspect behavior, but did not explicitly reject PEP 604 runtime-type errors.
- CI did not include a dedicated static guard for runtime-evaluated `|` type alias assignments in the desktop-packaged Python import graph.

## Fix
- Replaced runtime-evaluated PEP 604 alias usage in desktop-packaged import paths with Python 3.9-safe typing forms (`typing.Union` / `typing.Optional`).
- Strengthened packaged model bridge inspect probes to fail on:
  - `Model bridge failure`
  - `unsupported operand type(s) for |`
  - `No module named`
  - `ModuleNotFoundError`
  - `ImportError`
- Added AST-based regression guard to block runtime-evaluated PEP 604 alias assignments in desktop-packaged Python scope.

## Tests / prevention
- Unit guard for AST detection of runtime alias assignments.
- Unit import smoke for `utils.system.resource_monitor` in desktop path.
- Packaged inspect e2e now hard-fails on the macOS/Python 3.9 signature error text.
- macOS desktop workflow now runs no-relay-autostart lifecycle e2e after building the debug desktop app.

## Architecture invariant reaffirmed
Desktop-tauri remains an analogue of `server.py`. The app must not bundle, autolaunch, supervise, or own `relay.py`; relay lifecycle remains separate and may only be launched by explicit test harnesses when needed.
