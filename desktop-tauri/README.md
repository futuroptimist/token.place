# token.place desktop-tauri MVP

This folder contains a minimal Tauri desktop client vertical slice for token.place.

## MVP scope

- Bring-your-own `.gguf` model picker
- Runtime backend preference display:
  - `Metal / Apple Silicon` on macOS arm64
  - `CUDA / NVIDIA` on Windows x64
  - `CPU fallback` otherwise
- Prompt input, streamed output, and deterministic cancel
- Sidecar process boundary via `sidecar/mock_llama_sidecar.py`
- Optional encrypted forward of the final output to a relay-compatible `/sink` contract
- No plaintext prompt/output persistence by default

## Why a mock sidecar in this MVP

For the first merge, the sidecar contract is validated with a tiny NDJSON emitter so the UI,
state machine, cancellation path, and contract tests are stable in CI. Replacing the mock with a
Metal/CUDA-enabled llama.cpp wrapper is a follow-up that can reuse the same event schema.

## Local development

```bash
cd desktop-tauri
npm ci
npm run dev
```

## Platform assumptions

- **macOS Apple Silicon**: target llama.cpp Metal backend (displayed as `Metal / Apple Silicon`)
- **Windows 11 + NVIDIA**: target llama.cpp CUDA backend (displayed as `CUDA / NVIDIA`)
- **CPU fallback**: available on both when preferred GPU backend is unavailable

## Tests

```bash
cd desktop-tauri/src-tauri
cargo test
```
