# Outage: desktop release publish job failed on invalid action pin

- **Date:** 2026-04-29
- **Slug:** `desktop-release-publish-action-pin-invalid`
- **Affected area:** GitHub Actions desktop release pipeline (`.github/workflows/desktop-release.yml`)

## Summary
The **Publish Github Release** stage in the desktop Tauri release workflow failed
before execution because it referenced a non-existent commit SHA for
`softprops/action-gh-release`.

## Symptoms
- The job stopped during action resolution, before any release upload logic ran.
- GitHub Actions reported:
  - `Unable to resolve action softprops/action-gh-release@...`
  - `unable to find version ...`

## Impact
- Desktop release artifacts were not published to GitHub Releases.
- Release automation was blocked even though prior build/sign stages could
  succeed.

## Root cause
1. The workflow pinned `softprops/action-gh-release` to
   `153bb8e36b35d4f28c473603ceb4af68578f6b88`.
2. That SHA does not exist in the upstream action repository.
3. GitHub Actions fails closed when a pinned SHA cannot be resolved.

## Remediation
- Updated the action pin to the valid `v2.6.1` tag commit:
  `153bb8e04406b158c6c84fc1615b65b24149a1fe`.
- Applied the same correction in both release-related workflows to prevent
  recurrence in sibling pipelines.

## Follow-up / prevention
- For pinned actions, validate SHAs with `git ls-remote` against the action
  repository before merging.
- Keep action tag comments aligned with the pinned commit and periodically
  re-verify when workflow files are touched.
