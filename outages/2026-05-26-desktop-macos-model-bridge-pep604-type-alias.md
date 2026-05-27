# Outage: desktop macOS model bridge PEP 604 runtime type-alias failure

- **Date:** 2026-05-26
- **Slug:** `desktop-macos-model-bridge-pep604-type-alias`
- **Affected area:** Desktop Tauri model bridge startup/inspect flow on macOS Python 3.9 environments

## Summary
Desktop operator startup could fail with `Model bridge failure: unsupported operand type(s) for |: 'type' and 'type'` when the packaged runtime resolved to Python 3.9. The failure surfaced through `model_bridge.py` catch-all handling, but originated in a runtime-evaluated PEP 604 union type-alias assignment in `utils/system/resource_monitor.py`.

## Root cause
`GpuMetrics = Dict[str, float | int | bool]` was defined as a normal runtime assignment. On Python 3.9, evaluating `float | int | bool` at import time raises `TypeError` because PEP 604 runtime union operands are only supported in Python 3.10+.

## Why tests missed it
- Existing desktop packaged bridge tests primarily ran on Python 3.11.
- No static guard existed to detect runtime-evaluated `|` unions in assignments across the desktop-packaged Python import graph.

## Fix
- Replaced runtime alias usage with Python 3.9-safe typing in `utils/system/resource_monitor.py` (`Union[...]`).
- Added static AST guard coverage to fail on runtime `Assign` nodes containing PEP 604 `|` unions in the desktop-packaged Python import graph.
- Strengthened packaged inspect probe assertions to explicitly reject bridge/import/PEP604 failure signatures.
- Strengthened desktop UI e2e last-error assertions to reject `unsupported operand` / import failures / model-path-not-found markers.

## Relay lifecycle architecture note
This incident does **not** change architecture boundaries: the desktop Tauri app must not autolaunch, supervise, or own `relay.py`. Relay lifecycle remains separate and may only be started by explicit external harnesses (for example e2e fixtures).

## Verification and regression coverage
- Unit checks for `resource_monitor` and desktop bridge behavior.
- Static AST guard for runtime-evaluated PEP 604 alias assignments.
- Packaged model-bridge inspect probe rejects:
  - `Model bridge failure`
  - `unsupported operand type(s) for |`
  - `No module named`
  - `ModuleNotFoundError`
  - `ImportError`
- Existing no-relay-autostart guardrails remain in coverage.

## Follow-up
- Keep inspect-only packaged checks running under Python 3.9 in CI to catch compatibility regressions early.
- Continue treating desktop runtime compatibility and no-relay lifecycle invariants as release blockers for desktop changes.
