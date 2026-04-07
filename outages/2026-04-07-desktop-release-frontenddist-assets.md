# Desktop release workflow missing frontendDist assets (2026-04-07)

## Date
2026-04-07

## Summary
The desktop release workflow for Tauri attempted to package macOS and Windows
artifacts without first building the web frontend assets that Tauri expects at
`desktop-tauri/dist`.

## Impact
- `Build desktop artifacts (macos-latest)` failed.
- `Build desktop artifacts (windows-latest)` failed.
- Desktop release runs could not publish updated installers for either platform.

## Detection
- GitHub Actions run `actions/runs/24025934881` failed in both desktop build jobs.
- Tauri reported: `Unable to find your web assets, did you forget to build your web app?`
- `frontendDist` was configured as `../dist`, which resolves to
  `desktop-tauri/dist` from `desktop-tauri/src-tauri/tauri.conf.json`.

## Root cause
The workflow installed Node dependencies and then invoked `npm run tauri build`
directly, without an explicit frontend build step in CI to ensure the
configured `frontendDist` output existed before packaging.

## Resolution
- Updated `.github/workflows/desktop-release.yml` to run `npm run build` in
  `desktop-tauri/` before `npm run tauri build`.
- Upgraded `actions/checkout` and `actions/setup-node` to `@v5` in this
  workflow as a low-risk update aligned with Node 24 migration notices.

## Follow-up items
- Keep `desktop-release.yml` and other desktop workflows aligned on action major
  versions to avoid runtime deprecation churn.
- Add a CI guard that verifies `desktop-tauri/dist` exists before invoking
  Tauri packaging.
