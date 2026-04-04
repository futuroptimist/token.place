# token.place desktop-tauri MVP

This is the phase-1 Tauri MVP desktop client for token.place.

## What is included

- Single-screen Tauri UI to:
  - choose a local GGUF model path,
  - show preferred backend (`Metal / Apple Silicon`, `CUDA / NVIDIA`, or CPU fallback),
  - enter a prompt,
  - start streamed inference,
  - cancel in-flight inference,
  - encrypt + forward final output to relay `/faucet`.
- Replaceable sidecar boundary using NDJSON events.
- Minimal local config persistence for model path, relay URL, and compute mode.

## Targeted platforms in this MVP

- macOS Apple Silicon: defaults to `Metal / Apple Silicon`.
- Windows 11 x64 with NVIDIA GPUs: defaults to `CUDA / NVIDIA`.
- CPU fallback is available on both via override.

## Sidecar choice

This MVP uses a tiny custom sidecar protocol (`fake_sidecar.py` in-repo for dev/tests) because
it is the smallest path to deterministic streaming + cancellation with replaceable seams.
Production can swap to a llama.cpp wrapper or llama-server adapter without changing UI state logic.

## Run locally

```bash
cd desktop-tauri
npm run tauri:test
npm run tauri:dev
```

By default, the app invokes `python3 ../fake_sidecar.py` as the sidecar command.
You can change this in app config to point at a llama.cpp wrapper that emits NDJSON events.

