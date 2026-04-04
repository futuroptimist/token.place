# desktop (legacy Electron prototype)

This directory contains the original Electron-based token.place desktop prototype.

## Status

- **Deprecated / legacy**: do not start new feature work in this Electron implementation.
- **Forward-looking direction**: Tauri desktop client design in
  [`docs/design/tauri_desktop_client.md`](../docs/design/tauri_desktop_client.md).
- The Electron code is temporarily retained for historical reference while the Tauri
  implementation is built in follow-up PRs.

## What this means for contributors

- Prefer design and implementation work that aligns with the Tauri architecture.
- If you need to touch this folder, limit changes to maintenance-only updates that reduce
  confusion, improve docs, or support migration.
