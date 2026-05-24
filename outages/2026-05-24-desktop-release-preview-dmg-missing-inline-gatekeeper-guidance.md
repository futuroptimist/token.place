# Outage follow-up: desktop release preview DMG missing inline Gatekeeper guidance (2026-05-24)

- **Date:** 2026-05-24
- **Slug:** `desktop-release-preview-dmg-missing-inline-gatekeeper-guidance`
- **Affected area:** Desktop Tauri macOS Apple Silicon preview DMG UX

## Symptom

Users downloaded the correct Apple Silicon Tauri DMG and still hit:
"Apple could not verify \"token.place desktop\" is free of malware..."
after double-clicking the app.

## Root cause

This warning is expected for ad-hoc/non-notarized preview builds. Guidance existed
only as a sidecar release asset (`README-macos-apple-silicon-preview.txt`) and
was not visible inside the mounted DMG where users opened the app.

## Remediation

- Keep unpaid ad-hoc signing fallback for preview releases.
- Build DMGs from a staging folder that contains:
  - `token.place desktop.app`
  - inline opening guide (`README BEFORE OPENING.txt`)
  - `/Applications` symlink for drag-install flow.
- Keep publishing sidecar `README-macos-apple-silicon-preview.txt` in release assets.
- Validate DMG-root README presence and required guidance phrases during CI validation.

## Future optional path

To remove the warning entirely, use paid Apple Developer ID signing with
notarization/stapling in a future release pipeline.
