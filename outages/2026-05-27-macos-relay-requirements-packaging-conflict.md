# Outage: macOS relay quickstart dependency resolution failure (packaging conflict)

- **Date:** 2026-05-27
- **Status:** Resolved
- **Severity:** Medium

## Summary
Fresh macOS quickstart relay installs failed because `config/requirements_relay.txt` pinned `packaging==25.0`, while `Flask-Limiter==3.11.0` depends on `limits` versions that require `packaging<25`.

## Impact
- `python -m pip install -r config/requirements_relay.txt` failed with `ResolutionImpossible`.
- `python relay.py` then failed with `ModuleNotFoundError: No module named 'flask'` because dependencies were not installed.
- Affected new local setups, especially on macOS where users often bootstrap from a clean virtual environment.

## Root cause
A transitive compatibility mismatch was introduced between:
- direct pin: `packaging==25.0`
- transitive constraint via `Flask-Limiter` -> `limits` requiring `packaging<25`

## Fix
- Pinned relay dependency to `packaging==24.2` (compatible with the `limits` constraint chain).
- Updated quickstart documentation with macOS guidance to ensure the venv is created from Homebrew Python when `python3` still resolves to Apple-provided Python 3.9.

## Prevention
- Keep relay requirements aligned with resolver constraints of transitives used by pinned top-level packages.
- Validate quickstart in a clean venv on macOS for each dependency pin update.
