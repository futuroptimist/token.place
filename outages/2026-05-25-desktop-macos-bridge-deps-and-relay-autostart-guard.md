# 2026-05-25 desktop macOS bridge dependency preflight + no-relay-autostart guard

- **Date:** 2026-05-25
- **Area:** desktop-tauri operator startup and lifecycle

## Symptoms
- Start operator failed with early bridge exit and `No module named 'psutil'`.
- Separate regression report noted a phantom local relay process on `localhost:5010` after desktop app lifecycle.

## Root causes
1. Desktop bridge import chain reached `utils.system.resource_monitor` which imported `psutil` at module import time, so missing dependencies caused bridge exit before first startup event.
2. Desktop tests lacked explicit no-relay-autostart guardrails for bundle/startup sources and lifecycle assumptions.

## Why tests missed it
- Prior packaged checks validated imports but did not enforce explicit dependency preflight diagnostic payload before startup.
- No dedicated static guard test asserted desktop startup paths/bundle config never reference `relay.py`/`:5010`.

## Fix
- Added desktop dependency preflight in compute bridge startup using app-owned diagnostics (`interpreter`, `prefix`, and missing module set) before runtime boot path.
- Added reusable dependency verifier in `desktop_runtime_setup.py`.
- Added desktop no-relay-autostart static guard tests for Tauri bundle and startup sources.

## Tests
- `python -m pytest tests/unit/test_desktop_runtime_setup.py tests/unit/test_desktop_compute_node_bridge.py tests/unit/test_resource_monitor.py tests/unit/test_desktop_no_relay_autostart.py`
