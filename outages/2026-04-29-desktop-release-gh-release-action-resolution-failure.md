# Outage: Desktop release publish stage failed to resolve gh-release action

- **Date:** 2026-04-29
- **Slug:** `desktop-release-gh-release-action-resolution-failure`
- **Workflow run:** `actions/runs/25091837962`
- **Affected job:** `Publish GitHub Release` in `Desktop Tauri Release`

## Summary
The desktop release workflow failed before execution of release logic because GitHub Actions could not resolve the pinned commit SHA for `softprops/action-gh-release`.

## Impact
Desktop release artifacts were built, but release publication was blocked. This prevented automatic creation/update of the GitHub Release for that run.

## Detection
The job failed at action resolution time with:
- `Unable to resolve action softprops/action-gh-release@153bb8e36b35d4f28c473603ceb4af68578f6b88`
- `unable to find version 153bb8e36b35d4f28c473603ceb4af68578f6b88`

## Root cause
The workflows pinned `softprops/action-gh-release` to a specific commit SHA that was no longer resolvable by GitHub Actions at runtime.

**Unambiguous root cause statement (acceptance criteria):** release publish jobs referenced an unavailable action revision (`153bb8e36b35d4f28c473603ceb4af68578f6b88`), causing workflow startup failure before any release-publish steps ran.

## Resolution
Updated all desktop release publish workflow references from the unavailable commit pin to the maintained major tag `softprops/action-gh-release@v2`:
- `.github/workflows/desktop-release.yml`
- `.github/workflows/desktop-build.yml`

This restores action resolution and unblocks the publish stage.

## Why this fix works
`@v2` tracks a maintained release line in the upstream action repository rather than a single potentially unavailable revision. GitHub can resolve the major tag to a valid current v2 release, allowing workflow startup and release publication to proceed.

## Follow-up and prevention
- Add a periodic workflow dependency audit to detect unresolved or deprecated action pins before release runs.
- When pinning to SHAs, verify long-term availability strategy (e.g., vendor mirror or automation to refresh pins).
- Keep desktop release and desktop build workflows aligned so publish-action upgrades happen consistently.
