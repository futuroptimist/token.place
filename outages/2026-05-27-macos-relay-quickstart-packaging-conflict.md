# Outage: macOS relay quickstart packaging conflict

- **Date:** 2026-05-27
- **Slug:** `macos-relay-quickstart-packaging-conflict`
- **Affected area:** quickstart relay bootstrap (`config/requirements_relay.txt`, README quickstart)

## Summary
A fresh macOS setup following quickstart could create a virtual environment but failed while installing relay dependencies. `Flask-Limiter==3.11.0` resolves to `limits` versions that require `packaging<25`, while relay requirements pinned `packaging==25.0`.

## Symptoms
- `pip install -r config/requirements_relay.txt` failed with `ResolutionImpossible`.
- `python relay.py` failed with `ModuleNotFoundError: No module named 'flask'` because install never completed.

## Root cause
Pinned dependency conflict in relay requirements:
- Direct pin: `packaging==25.0`
- Transitive constraint: `limits` (via Flask-Limiter) requires `packaging<25`

## Remediation
- Changed relay dependency pin to `packaging==24.2` so pip can resolve `Flask-Limiter`/`limits` constraints.
- Updated quickstart and onboarding docs with a macOS/Homebrew guardrail: run `python -V` after activating `.venv`; if the venv reports Python 3.9 (or another too-old interpreter), recreate with `rm -rf .venv && /opt/homebrew/bin/python3 -m venv .venv`.

## Prevention
- Keep relay requirement pins aligned with transitive upper bounds.
- Treat interpreter verification as a quickstart guardrail (confirm `python -V` shows 3.11+) rather than the primary root cause for this outage.
