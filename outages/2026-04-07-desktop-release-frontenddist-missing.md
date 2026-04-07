# Desktop release outage: missing frontendDist assets in CI

- **Date:** 2026-04-07
- **Workflow run:** `actions/runs/24025934881`
- **Affected jobs:**
  - `Build desktop artifacts (macos-latest)`
  - `Build desktop artifacts (windows-latest)`

## Summary
The desktop release workflow failed during `tauri build` on macOS and Windows because
CI did not ensure the frontend assets existed at the path configured by
`frontendDist` before invoking the Tauri bundling step.

## Impact
Desktop release jobs failed, so new desktop installers could not be produced from a
clean checkout for both supported release platforms.

## Detection
The workflow logs reported:
- `Unable to find your web assets, did you forget to build your web app?`
- `frontendDist` resolved to `../dist`, which maps to:
  - `/Users/runner/work/token.place/token.place/desktop-tauri/dist` on macOS
  - `D:\a\token.place\token.place\desktop-tauri\dist` on Windows

## Root cause
`desktop-tauri/src-tauri/tauri.conf.json` points `build.frontendDist` to
`../dist` (the Vite output directory), but the release workflow called
`npm run tauri build` immediately after `npm ci` without an explicit frontend build
step to guarantee that `desktop-tauri/dist` existed in CI.

## Resolution
Updated `.github/workflows/desktop-release.yml` to:
1. run `npm run build` before `npm run tauri build`, and
2. bump `actions/checkout` and `actions/setup-node` from `@v4` to `@v5`
   (Node 24 runtime compatible) as a low-risk maintenance update.

## Follow-up items
- Add a CI assertion before Tauri packaging to check that `desktop-tauri/dist`
  exists and is non-empty.
- Consider adding a small release workflow smoke test that validates the full
  desktop build command sequence on tag-like refs.
