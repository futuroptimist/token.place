# Desktop release outage: stale Electron-style DMG naming and Gatekeeper confusion (2026-05-23)

## Summary
The desktop release page exposed a macOS asset named like a legacy Electron build
(`tokenplace Desktop-0.1.0-arm64.dmg`) rather than an explicit token.place Tauri
Apple Silicon artifact. This contributed to user confusion and coincided with
macOS Gatekeeper warnings that the app was "damaged and can't be opened".

## Impact
- Users could download a confusingly named macOS asset that did not clearly
  represent the intended Desktop Tauri Apple Silicon release.
- Some users saw a wrong/legacy icon presentation.
- Users received the macOS warning: "damaged and can't be opened. You should
  move it to the Trash."

## Symptoms
- macOS "damaged and can't be opened" warning on install/open.
- Wrong app icon relative to the intended Tauri icon set.
- Multiple/confusing release assets with stale Electron-style naming.

## Root cause
- Insufficient guardrails allowed stale/legacy Electron-style naming confusion
  in release artifact staging/publication.
- Release validations did not strongly enforce Tauri-only macOS artifact
  provenance or explicit Apple Silicon naming.
- Signing/notarization readiness signaling was insufficient for preview builds,
  so users could infer Gatekeeper-readiness when credentials were absent.

## Remediation
- Enforced Tauri-only macOS staging path from
  `desktop-tauri/src-tauri/target/.../bundle`.
- Standardized macOS DMG naming as
  `token.place-desktop-<version>-apple-silicon.dmg`.
- Added stale artifact/branding guardrails for:
  - `tokenplace Desktop`
  - `tokenplace Desktop Setup`
  - `desktop/electron-builder`
- Added icon validation against `desktop-tauri/src-tauri/icons/icon.icns`.
- Added Apple Silicon binary architecture validation (`arm64`/`aarch64`).
- Added signing verification path (`codesign` + `spctl`) when signing identity
  is present, and explicit preview/dev-only warning when absent.

## Prevention and regression tests
- Added unit tests that statically verify desktop release workflow guardrails
  and Tauri icon configuration.
- Added script-driven workflow validation for macOS staged artifacts.

## Manual verification checklist
1. Trigger Desktop Tauri Release workflow for a desktop tag.
2. Confirm release includes one obvious Apple Silicon DMG named:
   `token.place-desktop-<version>-apple-silicon.dmg`.
3. Confirm no release asset names contain stale Electron branding.
4. Mount/open `.app` and verify icon matches Tauri icon set.
5. Run `codesign --verify --deep --strict --verbose=2` and
   `spctl -a -vv --type execute` when signing credentials are configured.
6. If signing credentials are absent, ensure release documentation labels macOS
   build as preview/dev-only and not fully Gatekeeper-ready.
