# token.place desktop Tauri MVP

This folder contains the forward-looking Tauri desktop MVP for token.place.

## Scope of this MVP

- Single-screen UI for BYO GGUF model path + prompt entry.
- Runtime backend preference display:
  - macOS arm64 => `Metal / Apple Silicon`
  - Windows x64 => `CUDA / NVIDIA`
  - other targets => `CPU fallback`
- Sidecar-driven streaming output with explicit cancellation.
- Optional `Encrypt + forward output` action that sends the final output through
  the existing relay-compatible encrypted `/next_server` + `/faucet` flow.

## Why a fake sidecar for this slice?

To keep this PR vertical and small, the app uses a tiny NDJSON sidecar
(`sidecar/fake_llama_sidecar.py`) that models the interface we need for
llama.cpp integration (start/token/done/error/canceled) without requiring model
runtime packaging in CI.

The seam is intentionally replaceable: swap the sidecar executable and preserve
the JSON event contract.

## Run locally

```bash
cd desktop-tauri
npm ci
npm run tauri dev
```

## Cutting a desktop release

Desktop release builds are triggered by pushing a tag that matches
`desktop-v*` (for example `desktop-v0.1.0`).

```bash
git tag desktop-v0.1.0 <commit-sha>
git push origin desktop-v0.1.0
```

This triggers `.github/workflows/desktop-tauri-release.yml`, which builds and
publishes macOS + Windows Tauri installer artifacts to the matching GitHub
Release.

### Platform packaging assumptions (documented, not fully automated in MVP)

- **macOS Apple Silicon**: run with a Metal-enabled llama.cpp sidecar build.
- **Windows 11 + NVIDIA GPU**: run with a CUDA-enabled llama.cpp sidecar build.
- CPU fallback mode is available in both cases.

## Privacy defaults

- Prompt/response plaintext stays in-memory by default.
- The app only persists non-plaintext settings (model path, relay URL,
  preferred mode) in app-local config.
- Log lines are redacted to metadata (byte counts, request ids).
