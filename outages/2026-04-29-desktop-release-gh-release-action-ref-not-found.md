# Outage: Desktop release publish stage failed resolving pinned `action-gh-release` SHA

- **Date:** 2026-04-29
- **Slug:** `desktop-release-gh-release-action-ref-not-found`
- **Workflow run:** `actions/runs/25091837962`
- **Affected jobs:**
  - Publish GitHub Release (ubuntu-24.04)

## Summary
The Desktop Tauri Release workflow failed before the publish step executed because GitHub Actions could not resolve the exact commit SHA pinned for `softprops/action-gh-release`.

## Impact
Desktop artifacts were built, but the workflow could not create/update the GitHub Release. This blocked publishing installers from the release pipeline.

## Detection
Observed CI error:
- `Unable to resolve action 'softprops/action-gh-release@153bb8e36b35d4f28c473603ceb4af68578f6b88', unable to find version '153bb8e36b35d4f28c473603ceb4af68578f6b88'`

## Root cause
Both `.github/workflows/desktop-release.yml` and `.github/workflows/desktop-build.yml` pinned `softprops/action-gh-release` to commit `153bb8e36b35d4f28c473603ceb4af68578f6b88`. That ref is no longer resolvable by GitHub Actions for action download, causing setup to fail before workflow steps run.

**Unambiguous root cause statement (acceptance criteria):** release workflows depended on an action reference that GitHub could not fetch, so release publishing aborted during action resolution.

## Resolution
Updated both release workflows to use `softprops/action-gh-release@v2` (maintained major tag) instead of the non-resolvable commit SHA. This restores action resolution and unblocks release publishing.

## Lessons / follow-up
- Keep third-party action pins monitored and periodically validated.
- If strict commit pinning is required, add a scheduled check that validates all pinned action SHAs still resolve.
- Mirror critical release dependencies in a reusable workflow with centralized version management.
