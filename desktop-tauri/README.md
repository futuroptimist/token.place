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
  - Defaults to `src-tauri/python/inference_bridge.py`, which reuses the shared
    Python model runtime for real local inference.
  - Keeps `sidecar/fake_llama_sidecar.py` available via
    `TOKEN_PLACE_USE_FAKE_SIDECAR=1` (or `TOKEN_PLACE_SIDECAR_KIND=mock`) for
    CI and fast local tests.
- Optional `Encrypt + forward output` action that sends the final output through
  the existing relay-compatible encrypted `/next_server` + `/faucet` flow.

## Sidecar contract

Desktop inference consumes NDJSON events with this contract:

- `started`
- `token`
- `done`
- `canceled`
- `error`

`inference_bridge.py` emits this same contract while using the shared Python
runtime and model manager. The fake sidecar remains opt-in for CI/dev speed.

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
