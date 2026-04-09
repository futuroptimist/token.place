# token.place desktop Tauri MVP

This folder contains the forward-looking Tauri desktop MVP for token.place.

## Scope of this MVP

- Single-screen UI for BYO GGUF model path + prompt entry.
- Shows the canonical model family page and runtime GGUF artifact metadata from
  shared Python config/runtime logic.
- Lets users either browse to an existing GGUF or download the configured GGUF
  artifact into the shared models directory.
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

### Platform packaging assumptions (documented, not fully automated in MVP)

- **macOS Apple Silicon**: run with a Metal-enabled llama.cpp sidecar build.
- **Windows 11 + NVIDIA GPU**: run with a CUDA-enabled llama.cpp sidecar build.
- CPU fallback mode is available in both cases.

## Privacy defaults

- Prompt/response plaintext stays in-memory by default.
- The app only persists non-plaintext settings (model path, relay URL,
  preferred mode) in app-local config.
- Relay URL defaults to `https://token.place` and remains user-editable.
- Log lines are redacted to metadata (byte counts, request ids).

## Cutting a desktop release

Desktop binaries are published by the GitHub Actions workflow
`.github/workflows/desktop-release.yml`.

1. Create an explicit desktop tag on the commit you want to release:
   ```bash
   git tag desktop-v0.1.0 <commit-sha>
   git push origin desktop-v0.1.0
   ```
2. GitHub Actions builds `desktop-tauri/` artifacts on macOS and Windows and
   uploads them to the GitHub Release named `desktop-v0.1.0`.

You can also run the workflow manually with `workflow_dispatch` and provide
`tag_name` to rebuild/re-publish an existing desktop tag.
