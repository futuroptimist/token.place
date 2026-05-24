# 2026-05-24 macOS desktop compute-node startup dependency gap

## Summary
macOS desktop operator startup could fail before emitting a bridge startup event with `No module named 'requests'`.

## User impact
Users could click **Start operator** and see the operator stay stopped with a bridge startup error in the UI.

## Symptoms
- UI last-error area reported: `compute-node bridge exited before emitting a startup event: No module named 'requests'`.
- `Running` did not reach a stable started+registered state.

## Root cause
`utils/llm/model_manager.py` imported `requests` at module import time even though bridge startup only needed runtime/model metadata initialization and not the third-party client. In packaged/clean Python environments where `requests` was absent, import failed before startup events.

## Why existing tests missed it
Existing desktop tests ran in environments where `requests` was already installed, so they did not assert bridge startup-path imports under a blocked/clean dependency surface.

## Fix implemented
- Removed `requests` dependency from `utils/llm/model_manager.py` by switching model download I/O to `urllib.request`/`urllib.error` (standard library).
- Added a unit test that blocks `requests` imports and verifies `utils.llm.model_manager` still imports successfully.

## Tests added/repaired
- `tests/unit/test_model_manager_imports.py::test_model_manager_imports_without_requests_dependency`

## Follow-up
- Extend desktop operator start e2e to run in a stricter clean-environment Python job on macOS CI to catch undeclared bridge import dependencies earlier.
