# Desktop macOS model bridge PEP 604 runtime type alias failure (2026-05-26)

## Summary
Desktop Tauri model bridge startup could fail on macOS environments resolving to Python 3.9 with:
`Model bridge failure: unsupported operand type(s) for |: 'type' and 'type'`.

## Root cause
`utils/system/resource_monitor.py` used a runtime assignment type alias:
`GpuMetrics = Dict[str, float | int | bool]`.
This expression is evaluated at import time and is not Python 3.9-compatible, so importing desktop
model bridge dependencies could raise before inspect/download actions completed.

## Why tests missed it
Existing packaged bridge checks mainly exercised Python 3.11 and did not include a dedicated Python 3.9
inspect-only probe or a static guard for runtime-evaluated union aliases in desktop-packaged import paths.

## Fix
- Replaced runtime-evaluated union aliases in `resource_monitor.py` with Python 3.9-safe typing (`Union`, `Optional`).
- Added static AST regression guard for runtime-evaluated PEP 604 alias assignments in desktop-packaged Python graph.
- Strengthened packaged inspect probe assertions to reject bridge/import failures and PEP 604 type errors.
- Added macOS Python 3.9 inspect-only workflow coverage.
- Preserved desktop no-relay-autostart invariant: relay lifecycle remains test-harness-owned and separate from app ownership.

## Regression tests
- Unit/static guard for runtime-evaluated PEP 604 alias assignments.
- Packaged model bridge inspect probe rejects model bridge failure/import failure markers.
- Existing desktop no-relay-autostart unit guard remains active.

## Follow-up
- Keep desktop packaged import graph Python 3.9-safe while macOS launcher/runtime discovery can select 3.9.
- Maintain explicit relay separation: desktop app must not bundle/autolaunch/supervise `relay.py`.
