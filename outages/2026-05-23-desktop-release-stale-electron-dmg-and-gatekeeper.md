# Desktop release stale Electron DMG naming + Gatekeeper outage (2026-05-23)

## Summary
A Desktop Tauri release surfaced a confusing macOS asset name (`tokenplace Desktop-0.1.0-arm64.dmg`) and users hit a macOS Gatekeeper "damaged and can't be opened" dialog. We also observed wrong icon presentation relative to the expected Tauri icon set.

## Impact
- macOS Apple Silicon users were presented with unclear release assets.
- At least one downloaded DMG appeared untrustworthy to Gatekeeper.
- Icon mismatch reduced confidence in release authenticity.

## Symptoms
- macOS dialog: **"tokenplace Desktop is damaged and can’t be opened. You should move it to the Trash."**
- Wrong app icon instead of the `desktop-tauri/src-tauri/icons/` assets.
- Release page mixed naming patterns that looked like legacy Electron outputs.

## Root cause
- Stale/legacy Electron-style artifact naming leaked into release expectations and created ambiguity.
- Workflow guardrails were insufficient to fail fast on stale branding artifacts.
- Signing/notarization readiness was not explicitly surfaced for preview CI artifacts.

## Remediation
- Enforced Tauri-only macOS staging from `desktop-tauri/src-tauri/target/.../bundle`.
- Standardized obvious Apple Silicon DMG naming to `token.place-desktop-<version>-apple-silicon.dmg`.
- Added artifact/app metadata stale-branding checks (reject `tokenplace Desktop`, `tokenplace Desktop Setup`, `desktop/electron-builder`).
- Added architecture validation for app binary (`arm64`/`aarch64`) and x86_64-only rejection.
- Added icon validation to ensure bundled app icon aligns with `desktop-tauri/src-tauri/icons/icon.icns`.
- Added signing verification behavior:
  - enforce `codesign`/`spctl` when signing identity exists
  - emit explicit preview warning when signing credentials are absent

## Prevention / regression tests
- Unit tests now assert Desktop Tauri workflow staging path, Apple Silicon naming, stale-branding guardrails, and icon config expectations.

## Manual verification steps
1. Run the Desktop release workflow for a `desktop-v*` tag.
2. Confirm exactly one obvious Apple Silicon DMG appears: `token.place-desktop-<version>-apple-silicon.dmg`.
3. Mount DMG and verify app icon matches expected Tauri icon set.
4. Verify app binary architecture includes `arm64`.
5. If signing secrets are present, verify `codesign --verify --deep --strict --verbose=2` and `spctl -a -vv --type execute` pass.
6. If signing secrets are absent, confirm workflow emits unsigned preview warning and docs/release notes reflect preview status.
