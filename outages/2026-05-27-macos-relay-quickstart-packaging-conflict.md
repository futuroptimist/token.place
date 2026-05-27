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
- Changed relay dependency pin to `packaging==24.2`.
- Updated quickstart and onboarding docs to use `python3 -m venv .venv` rather than hardcoding `python3.12`, which is often absent on fresh Homebrew installs where `python3` points to current stable (for example Python 3.14).

## Prevention
- Keep relay requirement pins aligned with transitive upper bounds.
- Prefer `python3`/`py -3` for docs-first bootstrap commands unless an exact version is strictly required.
