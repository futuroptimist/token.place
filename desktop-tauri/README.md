# token.place desktop Tauri MVP

This folder contains the forward-looking Tauri desktop MVP for token.place.

## Scope of this MVP

- Single-screen UI with a background compute-node operator mode plus a local prompt smoke-test panel.
- Shows the canonical model family page and runtime GGUF artifact metadata from
  shared Python config/runtime logic.
- Lets users either browse to an existing GGUF or download the configured GGUF
  artifact into the shared models directory.
- Runtime backend preference controls expose `auto`, `metal`, `cuda`, and `cpu` modes.
- Background compute-node mode that registers and polls via `/sink`, decrypts requests, runs local inference, and posts responses to `/source` (or `/stream/source` when streaming is requested by the relay contract).
- Sidecar-driven local prompt smoke-test output with explicit cancellation.
- Optional debug-only relay forward action for manual `/next_server` + `/faucet` checks.

## Compute-node bridge behavior

Desktop includes a Python compute-node bridge
(`src-tauri/python/compute_node_bridge.py`) that reuses
`utils.compute_node_runtime` for the legacy relay `/sink` + `/source` flow used by `server.py`.
The bridge runs as the primary operator path and emits status events (running/registered, active relay URL, backend mode, model path, and last error).
Root `server.py` remains the canonical compute-node entrypoint; this desktop path must stay parity-aligned on the same legacy relay contract until post-parity API v1 migration work begins.

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

During normal startup, desktop sidecars run a runtime check in
`auto`/`gpu`/`hybrid` modes and emit:

- `desktop.runtime_setup ...` during sidecar start (backend selected + fallback reason)
- `compute_runtime ...` after `Llama(...)` init (backend actually used, offloaded
  layers, KV cache placement, and fallback reason)

On Windows desktop builds, `auto`/`gpu`/`hybrid` now attempt a one-time
`llama-cpp-python` CUDA repair automatically in the same interpreter used by
the sidecar (`sys.executable`) when the active runtime is CPU-only. The repair
uses the canonical README recipe:

```powershell
$env:CMAKE_ARGS = "-DGGML_CUDA=on"
$env:FORCE_CMAKE=1
pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

The sidecar then re-execs itself once so the repaired runtime is immediately
active for the same launch.

Set `TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP=1` to force probe-only
behavior.

For manual runtime verification, run:

```bash
python desktop-tauri/src-tauri/python/verify_llama_runtime.py auto
```

This prints `sys.executable`, `llama_cpp.__file__`, backend markers,
`llama_supports_gpu_offload`, and `ModelManager` compute diagnostics.

### Platform packaging assumptions (documented, not fully automated in MVP)

- **macOS Apple Silicon**: run with a Metal-enabled llama.cpp sidecar build.
- **Windows 11 + NVIDIA GPU**: run with a CUDA-enabled llama.cpp sidecar build.
- CPU fallback mode is available in both cases, with explicit fallback details
  surfaced in `desktop.runtime_setup ... fallback_reason=...` and
  `compute_runtime ... fallback_reason=...`.

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


The local prompt panel still uses `src-tauri/python/inference_sidecar.py` as a smoke test for local model setup.


## Desktop logging defaults

Desktop subprocess logs now default to high-signal output:

- Known low-value llama.cpp noise (metadata dumps, control-token spam, per-layer assignment spam, tensor repack spam) is filtered at the Tauri stderr forwarding boundary.
- Warnings and errors are always preserved in default mode.
- The inference sidecar emits concise stderr summary lines for model init and prompt/eval throughput.

To restore full raw subprocess output (including verbose llama.cpp logs), run desktop with either:

- `TOKEN_PLACE_VERBOSE_LLM_LOGS=1`
- `TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS=1`

Both flags are equivalent opt-in toggles and intended for troubleshooting.
