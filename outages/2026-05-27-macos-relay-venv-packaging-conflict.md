# Outage: macOS relay quickstart failed due to Python interpreter drift and packaging pin conflict

- **Date:** 2026-05-27
- **Slug:** `macos-relay-venv-packaging-conflict`
- **Affected area:** relay quickstart on macOS fresh Homebrew Python installs

## Summary
`python relay.py` failed during quickstart on macOS after `brew install python` because the created virtualenv still used Python 3.9 while relay requirements pinned `packaging==25.0`, which conflicts with Flask-Limiter's transitive `limits` dependency (`packaging<25`).

## Impact
Fresh relay setup could not install `config/requirements_relay.txt`, leaving Flask uninstalled and causing `ModuleNotFoundError: No module named 'flask'` when starting `relay.py`.

## Root cause
- Quickstart fallback command (`python3.12 -m venv ... || python3 -m venv ...`) can silently choose an older `python3` from PATH on some macOS environments.
- `config/requirements_relay.txt` pinned `packaging==25.0` while `Flask-Limiter==3.11.0` resolves to `limits` versions requiring `packaging<25`.

## Fix
- Pinned relay dependency to `packaging==24.2` to satisfy both `gunicorn` and `limits` constraints.
- Updated README/ONBOARDING/TESTING guidance to prefer explicit Python 3.12 or 3.11 interpreters and documented how to detect/recreate a venv when it accidentally uses Python 3.9.

## Verification
- `python3.12 -m venv .venv && source .venv/bin/activate && python -m pip install -r config/requirements_relay.txt` now resolves dependencies without `ResolutionImpossible`.
- `python relay.py` starts with Flask installed.

## Prevention
Keep relay requirement pins compatible with Flask-Limiter/limits transitive constraints, and require version check (`python -V`) after venv activation in macOS setup docs.
