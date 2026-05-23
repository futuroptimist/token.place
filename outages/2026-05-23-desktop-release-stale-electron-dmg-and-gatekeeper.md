# Desktop release outage: stale Electron-branded DMG confusion and macOS Gatekeeper failures (2026-05-23)

## Summary
Desktop release assets for `desktop-v0.1.0` were confusing and unreliable for macOS Apple Silicon users.
A DMG named like a legacy Electron output (`tokenplace Desktop-0.1.0-arm64.dmg`) was available in releases,
and users reported Gatekeeper “damaged and can’t be opened” failures plus a wrong app icon.

## Impact
- Apple Silicon users were not presented with one clear, trustworthy macOS desktop asset.
- Users saw stale `tokenplace Desktop` branding that did not match current `token.place desktop` branding.
- Unsigned/non-notarized artifacts could be interpreted as production-ready despite Gatekeeper risk.

## Symptoms
- macOS warning: “tokenplace Desktop is damaged and can’t be opened. You should move it to the Trash.”
- Wrong desktop app icon compared to the intended Tauri icon set.
- Release page asset naming ambiguity between legacy Electron style and Desktop Tauri output.

## Root cause
- Release staging and naming guardrails were insufficient to prevent stale/legacy Electron-style naming from
  being published or confused with Tauri assets.
- Signing/notarization readiness messaging for macOS artifacts was not explicit enough when Apple credentials
  were unavailable in CI.

## Remediation
- Enforced Tauri-only release staging from `desktop-tauri/src-tauri/target/.../bundle`.
- Enforced explicit Apple Silicon DMG naming:
  `token.place-desktop-<version>-apple-silicon.dmg`.
- Added stale branding checks that fail fast for:
  - `tokenplace Desktop`
  - `tokenplace Desktop Setup`
  - `desktop/electron-builder`
- Added macOS artifact validation for architecture (`arm64`/`aarch64`), bundled icon presence and icon hash match
  against `desktop-tauri/src-tauri/icons/icon.icns`, and stale metadata rejection.
- Added code-signing validation path (`codesign` + `spctl`) when signing identity/cert secrets are present.
- Added explicit unsigned preview warning path when signing materials are absent.

## Prevention and regression tests
- Added unit tests that statically validate desktop release workflow guardrails and icon config wiring.
- Added deterministic release artifact validation script used by workflow validation.

## Manual verification steps
1. Trigger `.github/workflows/desktop-release.yml` for a `desktop-v*` tag.
2. Confirm exactly one obvious Apple Silicon DMG appears:
   `token.place-desktop-<version>-apple-silicon.dmg`.
3. Confirm no staged/published artifacts include `tokenplace Desktop` or `desktop/electron-builder`.
4. Mount DMG and verify app icon and metadata match `token.place desktop` branding.
5. Verify app binary architecture includes `arm64`.
6. If signing credentials are configured, verify `codesign --verify --deep --strict` and `spctl -a -vv` pass.
   If not configured, confirm release notes/documentation mark build as preview/dev-only.
