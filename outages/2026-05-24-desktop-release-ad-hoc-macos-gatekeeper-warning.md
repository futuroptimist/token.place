# Outage: desktop release ad-hoc macOS Gatekeeper warning is expected for preview builds

- **Date:** 2026-05-24
- **Slug:** `desktop-release-ad-hoc-macos-gatekeeper-warning`
- **Affected area:** Desktop Tauri macOS Apple Silicon release preview artifacts

## Summary
The GitHub Releases macOS asset was correctly produced as a Tauri Apple Silicon
DMG with expected naming and icon validation, but users still saw
"Apple could not verify ..." Gatekeeper warnings after download.

## Symptom
- Downloading `token.place-desktop-<version>-apple-silicon.dmg` can show
  `"token.place desktop" Not Opened` and
  `Apple could not verify ...` warnings on macOS.

## Root cause
- The release path intentionally supports unpaid preview distribution via
  ad-hoc signing (or otherwise non-notarized artifacts) when paid Apple
  Developer ID/notarization credentials are not configured.
- This is distinct from stale Electron artifact issues; naming/path/icon and
  architecture checks can all pass while Gatekeeper still warns for
  non-notarized binaries.

## Decision
Unpaid preview releases remain acceptable and expected, provided release
messaging clearly states the trust model and manual-open steps.

## Remediation
- Keep ad-hoc signing fallback in CI for macOS releases when paid secrets are
  absent.
- Add a release-side warning asset (`README-macos-apple-silicon-preview.txt`)
  alongside the DMG explaining Gatekeeper behavior and manual-open instructions.
- Keep deterministic release validation for Tauri-only staging, Apple Silicon
  architecture, icon checks, checksum output, and stale Electron marker blocks.
- Update desktop-tauri docs to distinguish unpaid preview behavior from paid
  Developer ID + notarization distribution.

## Future optional path
A paid Apple Developer Program path can be added later for Developer ID signing
and notarization to enable standard no-warning distribution.
