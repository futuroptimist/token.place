# 2026-05-24 macOS desktop operator startup failure (`No module named 'requests'`)

## Summary
macOS desktop operator startup could fail before the bridge emitted its startup event when packaged/runtime Python could not import `requests`.

## User impact
- Clicking **Start operator** could immediately fail with:
  - `compute-node bridge exited before emitting a startup event: No module named 'requests'`
- Operator remained unavailable even though the UI startup error plumbing existed.

## Symptoms
- `Running` did not stabilize at `yes`.
- **Last error** showed a bridge startup/import failure.
- Failures reproduced in clean Python environments without contributor-local site-packages.

## Root cause
`compute_node_bridge.py` imports shared runtime modules (`utils.compute_node_runtime` -> `utils.networking.relay_client` and `utils.llm.model_manager`) that import `requests` at module import time. Packaged desktop Python resources did not include a deterministic `requests` runtime dependency.

## Why existing tests missed it
- Prior coverage depended on environments where `requests` was already present (global/site packages or dev venv), masking packaging/runtime dependency gaps.
- macOS packaged startup coverage existed but did not guarantee dependency independence from ambient Python installs in all paths.

## Fix implemented
- Added a bundled stdlib-backed `desktop-tauri/src-tauri/python/requests.py` compatibility module implementing the subset of `requests` used by bridge startup/runtime (`get`, `post`, `Response`, and request exceptions).
- Included the compatibility module in Tauri bundled resources.
- Updated packaged operator e2e resource layout fixture to include the same module so tests mirror packaged app behavior.

## Tests added/updated
- Updated Tauri resource-guard unit test to require `python/requests.py` in bundle resources.
- Updated packaged operator e2e layout fixture to copy `requests.py`, keeping clean-environment startup checks deterministic.

## Follow-up
- Continue converging bridge/runtime networking code onto explicit deterministic runtime surfaces so ambient Python packages cannot mask missing packaged dependencies.
