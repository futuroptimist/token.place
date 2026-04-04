# token.place desktop-tauri MVP

This folder contains the phase-1 vertical slice for the new Tauri desktop client.

## Scope in this MVP

- Single-screen prompt UI (model path, backend preference, prompt, stream output, cancel).
- Sidecar process boundary for inference using a replaceable NDJSON contract.
- Backend preference detection:
  - `Metal / Apple Silicon` on macOS arm64.
  - `CUDA / NVIDIA` on Windows x64.
  - `CPU fallback` otherwise.
- Optional explicit encrypt+forward action that uses the existing token.place encrypted
  request envelope (`encrypted`, `client_public_key`, `messages.{ciphertext,cipherkey,iv}`)
  against `/api/v1/public-key` + `/api/v1/chat/completions`.

## Sidecar choice

For this MVP we use a tiny custom sidecar protocol (`sidecar/fake_llama_sidecar.py`) that emits
line-delimited JSON events (`started`, `token`, `done`, `error`).

Why this for MVP:

- smallest reliable path for deterministic token streaming + cancellation in tests.
- keeps the boundary replaceable for a real llama.cpp/llama-server sidecar in follow-up work.

## Run locally

```bash
cd desktop-tauri
npm ci
npm run test
npm run dev
```

Notes:

- Bring-your-own model path only; model downloads are intentionally out of scope.
- No Linux support commitment in this MVP.
- For production acceleration, sidecar replacement should use:
  - llama.cpp Metal build on macOS Apple Silicon.
  - llama.cpp CUDA build on Windows 11 with NVIDIA GPUs.
