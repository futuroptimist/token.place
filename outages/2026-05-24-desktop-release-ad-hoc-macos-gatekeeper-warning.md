# Desktop release follow-up: ad-hoc macOS Gatekeeper warning is expected (2026-05-24)

## Summary
The Desktop Tauri Apple Silicon DMG now has the correct token.place artifact
name, icon, and architecture validation, but macOS can still warn:
"Apple could not verify ... is free of malware".

## Symptom
- GitHub Releases DMG `token.place-desktop-<version>-apple-silicon.dmg` is
  correct and passes project artifact guardrails.
- macOS may still block first-open with an Apple verification/Gatekeeper dialog.

## Root cause
- The preview path is ad-hoc signed and not notarized.
- This is a trust-policy limitation of unpaid/non-notarized distribution,
  not a stale Electron artifact issue.

## Decision
Unpaid preview releases remain acceptable if they are explicit, validated, and
accompanied by clear user-facing warnings/manual-open instructions.

## Remediation
- Keep ad-hoc fallback signing in CI when paid signing credentials are absent.
- Add a release-side warning asset (`README-macos-apple-silicon-preview.txt`)
  alongside the DMG.
- Document manual open flow (right-click Open, or System Settings -> Privacy &
  Security after first block).
- Preserve deterministic checks for Tauri-only artifacts, icon correctness,
  stale Electron marker rejection, and Apple Silicon binary validation.

## Optional future path
Paid Developer ID signing + notarization can be added later for no-warning
public macOS distribution, but it is not required for preview releases.
