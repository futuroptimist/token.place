# token.place desktop Tauri MVP

This folder contains the forward-looking Tauri desktop MVP for token.place.

## Scope of this MVP

- Single-screen UI with:
  - operator controls for running the desktop as a relay compute node, and
  - a local prompt smoke-test/debug panel.
- Shows the canonical model family page and runtime GGUF artifact metadata from
  shared Python config/runtime logic.
- Lets users either browse to an existing GGUF or download the configured GGUF
  artifact into the shared models directory.
- Runtime backend preference display:
  - macOS arm64 => `Metal / Apple Silicon`
  - Windows x64 => `CUDA / NVIDIA`
  - other targets => `CPU fallback`
- Sidecar-driven streaming output with explicit cancellation.
- Production operator flow now runs a background compute-node bridge that polls
  relay `/sink`, decrypts/processes requests with shared runtime code, and
  posts results to `/source` (or `/stream/source` when streaming is enabled).
- A legacy smoke action remains available for `/next_server` + `/faucet` relay
  testing and is explicitly labeled as non-production behavior.

## Compute node operator behavior

Desktop ships a Python bridge (`src-tauri/python/compute_node_bridge.py`) that
reuses the shared `utils.compute_node_runtime.ComputeNodeRuntime` extracted
from `server.py`.

Operator status surfaced in the UI:

- registered/running
- active relay URL
- backend mode
- model path
- last error

## Inference sidecar behavior

Desktop now defaults to a Python NDJSON bridge
(`src-tauri/python/inference_sidecar.py`) that reuses the shared
`utils.llm.model_manager` runtime and emits the existing
`started/token/done/canceled/error` event contract.

The fake sidecar remains available at `sidecar/fake_llama_sidecar.py` for CI
and fast local testing:

- Set `TOKEN_PLACE_USE_FAKE_SIDECAR=1` to explicitly opt into the fake sidecar.
- Set `TOKEN_PLACE_SIDECAR=/full/path/to/script.py` to explicitly override the
  sidecar command path (this takes precedence over
  `TOKEN_PLACE_USE_FAKE_SIDECAR`).

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
