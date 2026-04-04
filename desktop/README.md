# token.place desktop (legacy Electron prototype)

> Status: **Deprecated / frozen**

This directory currently contains the historical Electron prototype for token.place desktop.
It is kept for short-term reference while the project migrates to a Tauri-based desktop client.

## Forward-looking direction

Please do **not** start new feature work in this Electron implementation.

Use the Tauri design doc instead:

- [`docs/design/tauri_desktop_client.md`](../docs/design/tauri_desktop_client.md)

That design defines the planned local-LLM architecture (local inference, local encryption, and
forwarding encrypted results through `relay.py`).

## Retirement plan

Electron implementation retirement/removal will be handled in later PRs after the Tauri path
reaches baseline parity for required workflows.
