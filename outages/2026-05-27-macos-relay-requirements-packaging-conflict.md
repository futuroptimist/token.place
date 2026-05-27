# Outage: macOS relay quickstart dependency resolution conflict

- **Date:** 2026-05-27
- **Slug:** `macos-relay-requirements-packaging-conflict`
- **Affected area:** Relay quickstart dependency install (`config/requirements_relay.txt`) on fresh macOS/Homebrew Python environments

## Summary
Fresh relay setup on macOS failed during `python -m pip install -r config/requirements_relay.txt` with `ResolutionImpossible`.
The resolver reported `Flask-Limiter -> limits` requiring `packaging<25` while the file pinned `packaging==25.0`.

## Impact
Quickstart users could not finish relay dependency installation, so `python relay.py` failed early with `ModuleNotFoundError: No module named 'flask'`.

## Root cause
`config/requirements_relay.txt` pinned `packaging==25.0`, which conflicts with the `limits` versions allowed by `Flask-Limiter==3.11.0`.

## Fix
- Updated relay requirements to pin `packaging==24.2` so the dependency graph satisfies `Flask-Limiter` + `limits` constraints.
- Updated setup docs to call out recreating `.venv` after Python upgrades on macOS.
- Added explicit troubleshooting guidance in docs and README quickstart.

## Prevention
When bumping pinned shared dependencies, run a clean-environment relay install check (`python -m pip install -r config/requirements_relay.txt`) to catch resolver conflicts before release.
