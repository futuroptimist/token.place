# Desktop release outage: stale Electron-style DMG naming and Gatekeeper confusion (2026-05-23)

## Summary
A `desktop-v0.1.0` GitHub Release exposed a confusing macOS DMG asset name (`tokenplace Desktop-0.1.0-arm64.dmg`) and users reported Gatekeeper rejection (`...is damaged and can't be opened`). The release page also created icon/branding confusion versus the Tauri desktop icon set.

## Impact
- Apple Silicon users could download an artifact that looked like a legacy Electron output.
- Users saw a high-friction install failure path and unclear trust posture.
- Release page clarity for the Desktop Tauri distribution was degraded.

## Symptoms
- macOS prompt: **"...is damaged and can't be opened. You should move it to the Trash."**
- Wrong/unexpected app icon relative to `desktop-tauri/src-tauri/icons/`.
- Multiple/ambiguous desktop assets and stale naming cues.

## Root cause
- Stale legacy Electron-style naming/artifact leakage risk was not blocked at release staging.
- Release asset naming did not force an obvious Apple Silicon Tauri DMG identity.
- Signing/notarization state was not clearly surfaced as preview/dev-only when Apple credentials were absent.

## Remediation
- Enforced Tauri-only staging from `desktop-tauri/src-tauri/target/.../bundle` in Desktop Tauri release workflow.
- Standardized Apple Silicon DMG naming to `token.place-desktop-<version>-apple-silicon.dmg`.
- Added stale artifact/branding guardrails that fail the workflow on:
  - `tokenplace Desktop`
  - `tokenplace Desktop Setup`
  - `desktop/electron-builder`
- Added macOS validation for:
  - Apple Silicon executable architecture (`arm64`/`aarch64`)
  - bundled app icon presence and SHA256 match with `desktop-tauri/src-tauri/icons/icon.icns`
- Added signing validation split:
  - verify `codesign --verify --deep --strict --verbose=2` and `spctl -a -vv --type execute` when signing secrets/identity exist
  - explicit preview/dev-only warning when they do not

## Prevention and regression coverage
- New unit tests assert Desktop Tauri workflow pathing, naming guardrails, and icon config expectations.
- Workflow now includes deterministic staging validation script execution before publishing artifacts.

## Manual verification steps
1. Run the Desktop Tauri release workflow for a desktop tag (or workflow_dispatch with `tag_name`).
2. Confirm release assets include exactly one obvious Apple Silicon DMG: `token.place-desktop-<version>-apple-silicon.dmg`.
3. Mount/open the DMG and inspect app metadata:
   - app icon present under `Contents/Resources/icon.icns`
   - executable reports `arm64`/`aarch64`
4. Validate signing posture:
   - with Apple signing configured: `codesign` and `spctl` checks pass
   - without credentials: workflow emits explicit unsigned preview warning
5. Confirm no release asset names or metadata include stale Electron markers.
