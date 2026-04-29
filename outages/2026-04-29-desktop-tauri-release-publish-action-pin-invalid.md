# Outage: Desktop Tauri release publish stage failed due to invalid action pin

- **Date:** 2026-04-29
- **Slug:** `desktop-tauri-release-publish-action-pin-invalid`
- **Affected area:** GitHub Actions Desktop release publishing jobs
- **Introduction point:** GitHub Actions workflow pin update that referenced a non-existent commit SHA for `softprops/action-gh-release`

## Summary
The **Publish GitHub Release** stage in the Desktop Tauri release workflow failed before any release
logic executed because the workflow referenced an invalid commit SHA for
`softprops/action-gh-release`.

GitHub Actions could not resolve the pinned ref and aborted with:
`unable to find version 153bb8e36b35d4f28c473603ceb4af68578f6b88`.

## Severity / impact
- **Severity:** Medium (release automation outage)
- **User impact:** Desktop release artifacts were built but not published to GitHub Releases by the
  automated publish stage.
- **Scope:** Affected publish stages using the same invalid `action-gh-release` SHA pin.

## Root cause
- The workflow pinned `softprops/action-gh-release` to a SHA that does not exist upstream:
  `153bb8e36b35d4f28c473603ceb4af68578f6b88`.
- Because action resolution happens in the runner setup phase, the job failed immediately during
  action download metadata resolution.

## What failed technically
- Job: Desktop Tauri Release → Publish GitHub Release
- Failure point: "Getting action download info"
- Error: `Unable to resolve action ... unable to find version ...`

This prevented execution of all subsequent release-publish steps.

## Fix implemented
- Replaced the invalid SHA with the valid `v2.6.1` pinned commit for
  `softprops/action-gh-release`:
  - from `153bb8e36b35d4f28c473603ceb4af68578f6b88`
  - to `153bb8e04406b158c6c84fc1615b65b24149a1fe`
- Applied this correction consistently in both workflows that publish desktop releases:
  - `.github/workflows/desktop-release.yml`
  - `.github/workflows/desktop-build.yml`

## Verification
- Confirmed the invalid SHA no longer appears in repository workflow files.
- Confirmed the corrected SHA is present in both desktop release publish workflows.

## Prevention / follow-up
- Keep action pins sourced from upstream release tags and verify pinned SHA existence when updating
  workflow actions.
- Add a workflow lint/check step for action pin validity to catch unresolved action refs before
  release day.
