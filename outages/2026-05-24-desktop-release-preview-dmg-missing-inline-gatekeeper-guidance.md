# Outage follow-up: desktop release preview DMG missing inline Gatekeeper guidance (2026-05-24)

- **Date:** 2026-05-24
- **Slug:** `desktop-release-preview-dmg-missing-inline-gatekeeper-guidance`
- **Affected area:** Desktop Tauri macOS Apple Silicon release packaging and validation (`.github/workflows/desktop-release.yml`, `scripts/validate_desktop_tauri_release_artifacts.py`)

## Symptom

The corrected Tauri Apple Silicon preview DMG still showed “Apple could not verify ...”
when users double-clicked `token.place desktop.app`.

## Root cause

Ad-hoc/non-notarized preview builds are expected to trigger Gatekeeper, but the
manual-open instructions existed only as a sidecar GitHub release asset. The
mounted DMG itself did not include visible guidance.

## Remediation

- Build the DMG from a staging directory that includes:
  - `token.place desktop.app`
  - `README BEFORE OPENING.txt`
  - `Applications` symlink for drag-install UX.
- Keep publishing `README-macos-apple-silicon-preview.txt` as a release-side asset.
- Validate mounted DMG contents in CI: exactly one app, preview README at DMG root,
  and required guidance phrases (`ad-hoc signed`, `not notarized`, `Apple could not verify`,
  `Privacy & Security`, `Developer ID`, `notarization`).

## Future optional path

To remove Gatekeeper warnings entirely, migrate to paid Developer ID signing plus
notarization/stapling.
