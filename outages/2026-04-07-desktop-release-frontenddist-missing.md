# Outage: desktop-release frontendDist assets missing in CI

- **Date:** 2026-04-07
- **Slug:** `desktop-release-frontenddist-missing`
- **Workflow run:** `actions/runs/24025934881`
- **Affected jobs:**
  - Build desktop artifacts (macos-latest)
  - Build desktop artifacts (windows-latest)

## Summary
The desktop release GitHub Actions workflow failed while packaging Tauri desktop artifacts because the frontend build output expected by Tauri was not present in CI at build time.

## Impact
Desktop release builds for macOS and Windows did not produce publishable installer artifacts from the release workflow.

## Detection
Observed CI errors:
- `Unable to find your web assets, did you forget to build your web app?`
- `frontendDist is set to "../dist"`

Missing paths in failed runners:
- `/Users/runner/work/token.place/token.place/desktop-tauri/dist`
- `D:\a\token.place\token.place\desktop-tauri\dist`

## Root cause
`desktop-tauri/src-tauri/tauri.conf.json` configures `build.frontendDist` as `../dist`, which requires `desktop-tauri/dist` to exist before packaging.

**Unambiguous root cause statement (acceptance criteria):** the workflow invoked `tauri build` without first ensuring the configured `frontendDist` assets existed in CI.

## Resolution
Updated `.github/workflows/desktop-release.yml` to run `npm run build` in `desktop-tauri` before `npm run tauri build`, ensuring `desktop-tauri/dist` exists when Tauri validates `frontendDist`. Also upgraded low-risk GitHub Actions versions in that workflow: `actions/checkout@v4` to `actions/checkout@v5`, and `actions/setup-node@v4` to `actions/setup-node@v5`.

## Lessons / follow-up
- Keep an explicit frontend pre-build in release CI for clean runners.
- Keep workflow dependencies current to avoid avoidable release failures.
