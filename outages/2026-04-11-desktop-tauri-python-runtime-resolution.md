# Outage: desktop-tauri operator startup failed when Python launcher was unresolved

- **Date:** 2026-04-11
- **Slug:** `desktop-tauri-python-runtime-resolution`
- **Affected area:** desktop Tauri compute-node operator startup (`Start operator`)

## Summary
On Windows operator workstations, clicking **Start operator** briefly toggled `Running: yes`
and then immediately returned to `Running: no`. The bridge process exited right away with
`Python was not found`, leaving operators unable to register with the relay.

## Symptoms
- `Running` changed to `yes` and then back to `no` within seconds.
- `Last error` in UI showed bridge exit status (for example, `exit code: 9009`).
- Command window stderr showed: `Python was not found; run without arguments to install...`.

## Impact
Desktop operators could not start unless Python command names happened to match local PATH
configuration, causing startup failures on otherwise valid desktop installs.

## Root cause
The Tauri host launched Python sidecars using a single hard-coded default command for `.py`
entrypoints (`python3` in sidecar/compute paths, `python` in model bridge path). On Windows,
that launcher name is not consistently available and may resolve to app-execution alias stubs that
exit immediately with "Python was not found".

## Remediation
- Added shared Python runtime resolution in `src-tauri/src/python_runtime.rs`.
- Runtime resolver now:
  - honors explicit env override first (`TOKEN_PLACE_SIDECAR_PYTHON` / `TOKEN_PLACE_PYTHON`),
  - probes candidate launchers with `--version`,
  - uses Windows fallback order `py -3`, `python`, `python3`,
  - uses non-Windows fallback order `python3`, `python`,
  - returns actionable error text if no working Python 3 runtime is available.
- Updated **both** operator/inference sidecar launchers and model bridge launcher to use the
  shared resolver.

## Follow-up / prevention
- Keep all Python subprocess startup paths centralized in one resolver.
- Add regression tests for candidate ordering and env override precedence.
- During release QA on Windows, validate operator start with and without PATH `python` alias.
