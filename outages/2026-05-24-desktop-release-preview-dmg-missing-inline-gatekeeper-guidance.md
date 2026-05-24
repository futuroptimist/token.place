# Outage follow-up: desktop release preview DMG missing inline Gatekeeper guidance (2026-05-24)

- **Date:** 2026-05-24
- **Slug:** `desktop-release-preview-dmg-missing-inline-gatekeeper-guidance`
- **Affected area:** Desktop Tauri macOS Apple Silicon release packaging and validation

## Symptom

The release used the correct Apple Silicon Tauri DMG and app naming/icon, but
users who double-clicked the app still saw the expected Gatekeeper dialog:
"Apple could not verify \"token.place desktop\" is free of malware."

## Root cause

The build path is intentionally ad-hoc signed and not notarized when paid Apple
Developer credentials are absent, so Gatekeeper prompts are expected. Guidance
was only present as a sidecar release asset and not visible inside the mounted
DMG where users actually open the app.

## Remediation

- Build the DMG from a staging directory containing:
  - `token.place desktop.app`
  - `README BEFORE OPENING.txt` with manual allow/open instructions
  - `Applications` symlink for drag-install UX
- Keep `README-macos-apple-silicon-preview.txt` as a release sidecar asset.
- Extend artifact validation to confirm mounted DMG includes the README with
  expected ad-hoc/notarization and Privacy & Security guidance text.

## Future optional path

A no-warning distribution path still requires paid Developer ID signing plus
notarization. This change only improves unpaid preview clarity and does not
attempt to bypass Gatekeeper.
