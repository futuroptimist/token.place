# Outage follow-up: desktop release ad-hoc macOS Gatekeeper warning (2026-05-24)

- **Date:** 2026-05-24
- **Slug:** `desktop-release-ad-hoc-macos-gatekeeper-warning`
- **Affected area:** Desktop Tauri macOS Apple Silicon release artifacts (`.github/workflows/desktop-release.yml`)

## Symptom

Even when the release DMG name/icon/architecture were corrected for Tauri Apple
Silicon (`token.place-desktop-<version>-apple-silicon.dmg`), macOS still showed
an "Apple could not verify ..." Gatekeeper warning after download.

## Root cause

The warning came from expected ad-hoc/non-notarized preview distribution, not
from stale Electron artifacts or incorrect Tauri packaging.

## Decision

Unpaid preview releases remain acceptable:

- keep ad-hoc signing fallback enabled,
- do not require paid Apple Developer ID certificates or notarization secrets,
- clearly label preview behavior so users expect Gatekeeper prompts.

## Remediation

- Kept ad-hoc signing fallback (`TAURI_BUNDLE_MACOS_SIGNING_IDENTITY='-'`) when
  Developer ID credentials are absent.
- Added a staged macOS release text asset:
  `README-macos-apple-silicon-preview.txt`.
- Documented manual open paths through Control-click/Open and
  System Settings → Privacy & Security.
- Kept deterministic artifact validation guardrails for naming, icon, and Apple
  Silicon architecture.

## Future optional path

A no-warning public path can be added later with paid Developer ID signing plus
Apple notarization/stapling. That remains optional and is not required for the
current preview release channel.
