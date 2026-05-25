# Outage: desktop macOS operator startup dependency gap and relay autostart guard regression

- **Date:** 2026-05-25
- **Slug:** `desktop-macos-runtime-deps-and-relay-autostart-guard`
- **Affected area:** desktop-tauri operator startup and desktop app process lifecycle

## Summary
On packaged-like macOS launches, `Start operator` could fail early with `compute-node bridge exited before emitting a startup event: No module named 'psutil'`.
A separate regression concern also required hard guards to ensure desktop-tauri never bundles or autostarts `relay.py` and never owns localhost:5010 relay lifecycle.

## Root causes
- The bridge import chain pulled `utils.system.resource_monitor` during startup, and `psutil` was imported at module import time.
- Desktop bridge startup used `PYTHONNOUSERSITE=1` and could resolve to interpreters missing desktop runtime deps.
- Existing coverage validated import/layout paths but did not enforce a desktop-specific dependency preflight contract and explicit no-relay-autostart desktop guardrails.

## Why tests missed it
- Packaged bridge checks did not require an app-owned dependency preflight before bridge runtime imports.
- No dedicated desktop static guard test existed to assert that Tauri resources and Rust desktop startup sources exclude `relay.py` autostart paths.

## Fix
- Added desktop runtime dependency manifest and bootstrap preflight in `desktop_runtime_setup.py` for packaged/runtime contexts.
- Made resource monitor resilient to missing `psutil` (metrics degrade safely).
- Added desktop static guard tests that prevent `relay.py` inclusion/spawn paths from desktop-tauri resources and startup sources.

## Tests
- `python -m pytest tests/unit/test_desktop_runtime_setup.py tests/unit/test_resource_monitor.py tests/unit/test_desktop_no_relay_autostart.py`
- `TOKEN_PLACE_INSPECT_ONLY=1 python desktop-tauri/scripts/test_packaged_operator_e2e.py`

## Follow-up
- Keep desktop dependency preflight manifest in sync with bridge/runtime imports.
- Extend full lifecycle macOS app open/close e2e in environments where GUI automation is available.
