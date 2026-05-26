# 2026-05-26 — macOS desktop model bridge failed on runtime-evaluated PEP 604 alias

## Summary
macOS desktop operator startup/inspect could fail with `Model bridge failure: unsupported operand type(s) for |: 'type' and 'type'` when packaged runtime resolved to Python 3.9.

## User impact
Users running desktop builds backed by Python 3.9 could see **Start operator** fail early and inspect/reporting paths return model bridge failures before runtime startup.

## Symptoms
- `model_bridge.py inspect` failed through the catch-all `Model bridge failure` path.
- Error text included `unsupported operand type(s) for |`.
- Startup status failed to reach stable `Running: yes` / `Registered: yes` in affected environments.

## Root cause
`utils/system/resource_monitor.py` defined a runtime assignment alias using PEP 604 unions:
`GpuMetrics = Dict[str, float | int | bool]`.
That expression is evaluated at import time; on Python 3.9 this raises `TypeError` because `|` on built-in types is unsupported there. The desktop model bridge import chain includes `utils.llm.model_manager` -> `utils.system.resource_monitor`, so the failure surfaced as a model bridge failure.

## Why tests missed it
Coverage exercised bridge behavior but lacked a guard for runtime-evaluated union aliases in desktop-packaged import paths and did not explicitly reject this error family in packaged inspect probes.

## Fix implemented
- Replaced runtime alias/return types in `resource_monitor.py` with Python 3.9-safe `typing.Union` forms.
- Added static AST guard coverage to fail if runtime alias assignments use PEP 604 unions in desktop-packaged import graph roots.
- Hardened packaged inspect e2e assertions to explicitly reject:
  - `Model bridge failure`
  - `unsupported operand type(s) for |`
  - `No module named`
  - `ModuleNotFoundError`
  - `ImportError`
- Preserved desktop no-relay-autostart invariant and wired macOS lifecycle no-relay e2e into desktop CI so lifecycle guard is not skip-only.

## Tests added/repaired
- `tests/unit/test_resource_monitor.py` regression for the `GpuMetrics` alias text form.
- `tests/unit/test_desktop_python_pep604_runtime_alias_guard.py` AST/source guard.
- Updated `desktop-tauri/scripts/test_packaged_operator_e2e.py` inspect probe output rejection list.
- Updated `.github/workflows/desktop-operator-e2e.yml` to build macOS debug app and run `test_desktop_no_relay_autostart_e2e.py`.

## Follow-up items
- Keep a Python 3.9 inspect-only CI job for desktop-packaged import graph probes where feasible.
- Continue enforcing that desktop app does not bundle/autolaunch/supervise `relay.py`; relay lifecycle remains separate and may only be launched by explicit test harnesses.
