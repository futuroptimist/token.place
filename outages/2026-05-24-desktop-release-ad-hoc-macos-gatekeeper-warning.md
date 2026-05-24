# Desktop release follow-up: ad-hoc macOS Gatekeeper warning messaging (2026-05-24)

## Summary
The Apple Silicon DMG now has the correct Tauri artifact name, icon, and architecture,
but users can still see "Apple could not verify ..." after downloading from GitHub Releases.
That warning is expected for ad-hoc signed, non-notarized preview distribution.

## Symptom
- Downloaded `token.place-desktop-<version>-apple-silicon.dmg` opens to a correctly
  branded app, but macOS shows Gatekeeper warning text such as
  "Apple could not verify ..." on first launch.

## Root cause
- The build is intentionally on the unpaid ad-hoc signing path and is not notarized.
- This is not a stale Electron artifact issue when DMG naming/icon/architecture
  validations pass.

## Decision
- Keep unpaid preview releases enabled and acceptable for Apple Silicon DMGs.
- Do not require paid Apple Developer Program credentials for preview release
  artifact generation.
- Clearly warn users that Gatekeeper warnings are expected and provide manual
  open steps.

## Remediation
- Ensure workflow keeps `TAURI_BUNDLE_MACOS_SIGNING_IDENTITY='-'` fallback when
  Developer ID signing credentials are absent.
- Generate and publish `README-macos-apple-silicon-preview.txt` alongside the DMG
  to explain ad-hoc/non-notarized behavior and manual open instructions.
- Preserve deterministic release guardrails: Tauri-only staging, expected icon,
  Apple Silicon binary validation, checksum output, and stale Electron marker
  rejection.

## Optional future path
- Add paid Developer ID signing + notarization for public no-warning distribution
  if and when project policy chooses that path.
