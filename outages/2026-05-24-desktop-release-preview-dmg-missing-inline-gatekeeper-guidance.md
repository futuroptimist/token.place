# Outage follow-up: desktop release preview DMG missing inline Gatekeeper guidance (2026-05-24)

- **Date:** 2026-05-24
- **Slug:** `desktop-release-preview-dmg-missing-inline-gatekeeper-guidance`
- **Affected area:** Desktop Tauri macOS Apple Silicon release packaging (`.github/workflows/desktop-release.yml`)

## Symptom

The Apple Silicon DMG artifact was correct (Tauri app, expected icon/name), but users who double-clicked the app in the mounted DMG still saw:

"Apple could not verify \"token.place desktop\" is free of malware."

## Root cause

This behavior is expected for ad-hoc/non-notarized preview releases. The release pipeline only surfaced guidance in a sidecar release asset (`README-macos-apple-silicon-preview.txt`), while the mounted DMG itself contained only the app, so users naturally encountered Gatekeeper first without inline instructions.

## Remediation

- Kept unpaid ad-hoc signing fallback (`TAURI_BUNDLE_MACOS_SIGNING_IDENTITY='-'`).
- Added DMG staging directory so mounted DMG contains:
  - `token.place desktop.app`
  - `README BEFORE OPENING.txt` with explicit manual-open guidance
  - `/Applications` symlink for expected drag-install UX
- Kept sidecar release asset generation (`README-macos-apple-silicon-preview.txt`).
- Hardened artifact validation to mount DMG read-only on macOS CI and assert README presence/content with key phrases (`ad-hoc signed`, `not notarized`, `Apple could not verify`, `Privacy & Security`, `Developer ID`, `notarization`).

## Future optional path

To remove Gatekeeper warning dialogs entirely, move to paid Apple Developer ID signing plus notarization/stapling. This remains optional and is not required for unpaid preview distribution.
