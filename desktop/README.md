# desktop (legacy Electron prototype)

This directory contains a **deprecated Electron-based prototype** for token.place.

## Status

- **Current status:** frozen / legacy
- **Do not add new feature work** in this Electron implementation.
- **Forward-looking direction:** Tauri desktop client

See the active design doc:
- [`docs/design/tauri_desktop_client.md`](../docs/design/tauri_desktop_client.md)

## Why this exists

The Electron code remains in-repo for historical context while token.place migrates to a Tauri
architecture focused on:

- local llama.cpp-backed inference
- local encryption before network egress
- encrypted forwarding through existing `relay.py` contracts

Retirement/removal of this legacy implementation is planned for a later implementation task once
Tauri reaches minimum parity.
