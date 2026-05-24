# 2026-05-24 — macOS desktop operator startup failed on missing `requests`

## Summary
macOS desktop operator startup could fail before registration when the packaged compute-node bridge attempted to import Python's third-party `requests` package in an environment where it was not bundled.

## User impact
Desktop users saw startup fail and operator never reached a stable running/registered state.

## Symptoms
- UI “Last error” showed: `compute-node bridge exited before emitting a startup event: No module named 'requests'`.
- `Running` did not remain healthy because bridge startup terminated early.

## Root cause
Bridge startup imports `utils/networking/relay_client.py`, which imports `requests` at module import time. Packaged desktop startup did not guarantee a bundled `requests` dependency for that runtime path.

## Why tests missed it
Prior coverage did not enforce a bridge-only import environment that excludes system/global Python packages while still executing the packaged startup path.

## Fix implemented
- Added a desktop-bridge-local `requests` compatibility shim implemented with Python stdlib (`urllib`) under `desktop-tauri/src-tauri/python/requests.py`.
- Updated packaged operator e2e layout to include this shim and force bridge/import probes to run with packaged-resource-first `PYTHONPATH`.

## Tests added/repaired
- Updated `desktop-tauri/scripts/test_packaged_operator_e2e.py` so packaged-mode probes and startup execution prioritize packaged resources and detect missing undeclared modules in the bridge path.

## Follow-up
- Keep bridge startup dependency checks in packaged-mode e2e to prevent regressions where startup depends on non-packaged third-party modules.
