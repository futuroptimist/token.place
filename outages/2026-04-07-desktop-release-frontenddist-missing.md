# Desktop release CI missing frontend assets

- **Date:** 2026-04-07
- **Workflow run:** `actions/runs/24025934881`
- **Affected jobs:**
  - `Build desktop artifacts (macos-latest)`
  - `Build desktop artifacts (windows-latest)`

## Summary
The desktop release GitHub Actions workflow failed while packaging Tauri desktop artifacts because the frontend build output expected by Tauri was not present in CI at build time.

## Impact
Desktop release builds for macOS and Windows did not produce publishable installer artifacts from the release workflow.

## Detection
The release workflow failed with:

- `Unable to find your web assets, did you forget to build your web app?`
- `frontendDist is set to "../dist"`

The resolved missing paths were:

- `/Users/runner/work/token.place/token.place/desktop-tauri/dist`
- `D:\a\token.place\token.place\desktop-tauri\dist`

## Root cause
`desktop-tauri/src-tauri/tauri.conf.json` configures `build.frontendDist` as `../dist`, which requires `desktop-tauri/dist` to exist before packaging. The desktop release workflow installed dependencies and invoked `npm run tauri build` without an explicit frontend build step to guarantee those assets exist in a clean checkout CI environment.

## Resolution
Updated `.github/workflows/desktop-release.yml` to run `npm run build` in `desktop-tauri` before `npm run tauri build`, ensuring `desktop-tauri/dist` exists when Tauri validates `frontendDist`.

Also upgraded low-risk GitHub Actions versions in that workflow:

- `actions/checkout@v4` → `actions/checkout@v5`
- `actions/setup-node@v4` → `actions/setup-node@v5`

## Follow-up items
- Consider adding a fast preflight step that asserts `desktop-tauri/dist/index.html` exists before invoking `tauri build`.
- Keep Actions dependencies current to avoid deprecation warnings around runner Node runtime changes.
