# Desktop release outage: missing Tauri frontend assets in CI

- **Date:** 2026-04-07
- **Workflow run:** `actions/runs/24025934881`

## Summary
The desktop release workflow failed for `macos-latest` and `windows-latest`
when it invoked `npm run tauri build` without first ensuring the frontend build
artifacts existed at the configured `frontendDist` path.

## Impact
Desktop release jobs failed before artifact packaging, blocking publication of
new macOS and Windows desktop installers from the release workflow.

## Detection
GitHub Actions `Desktop Tauri Release` run `actions/runs/24025934881` reported:

- `Build desktop artifacts (macos-latest)` failed
- `Build desktop artifacts (windows-latest)` failed
- Tauri error: `Unable to find your web assets, did you forget to build your web app?`
- `frontendDist` configured as `../dist`, resolving to:
  - `/Users/runner/work/token.place/token.place/desktop-tauri/dist`
  - `D:\a\token.place\token.place\desktop-tauri\dist`

## Root cause
`desktop-tauri/src-tauri/tauri.conf.json` expects prebuilt frontend files in
`../dist` relative to `src-tauri`. The release workflow installed dependencies
and immediately ran `npm run tauri build` without an explicit prior
`npm run build` step to guarantee `desktop-tauri/dist` exists in a clean CI
checkout.

## Resolution
Updated `.github/workflows/desktop-release.yml` to build frontend assets before
running the Tauri bundling step:

1. `npm ci`
2. `npm run build`
3. `npm run tauri build` (or target-specific variant)

Also bumped `actions/checkout` and `actions/setup-node` from `@v4` to `@v5` in
this workflow to align with the Node 24 migration warning.

## Follow-up items
- Add a release-workflow validation check that asserts `desktop-tauri/dist`
  exists before Tauri packaging.
- Keep GitHub Actions major versions current across workflows to avoid runtime
  deprecation churn.
