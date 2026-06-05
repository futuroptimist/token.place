# token.place desktop Tauri MVP

This folder contains the forward-looking Tauri desktop MVP for token.place.

## Scope of this MVP

- Single-screen UI with a background compute-node operator mode plus a local prompt smoke-test panel.
- Shows the canonical model family page and runtime GGUF artifact metadata from
  shared Python config/runtime logic.
- Lets users either browse to an existing GGUF or download the configured GGUF
  artifact into the shared models directory.
- Runtime backend preference controls expose `auto`, `metal`, `cuda`, and `cpu` modes.
- Background compute-node mode that warm-loads the local runtime, registers through API v1 E2EE relay routes, polls for encrypted work, runs local inference, and submits encrypted non-streaming responses.
- Sidecar-driven local prompt smoke-test output with explicit cancellation.
- Shared Windows/macOS operator lifecycle behavior for warm-load, register, multi-turn chat, Stop, Start after Stop, and diagnostics.

## Compute-node bridge behavior

Desktop includes a Python compute-node bridge
(`src-tauri/python/compute_node_bridge.py`) that reuses shared runtime and API v1 E2EE relay logic for desktop compute-node work. The bridge runs as the primary operator path and emits status events (running/registered, active relay URL, backend mode, model path, warm-load state, runtime backend fields, and last error).
Root `server.py` remains the canonical non-desktop compute-node entrypoint; desktop behavior must stay parity-aligned with the shared API v1 E2EE relay contract and must not reintroduce legacy relay endpoints or API v1 streaming.

See [Desktop parity validation checklist](../docs/desktop_parity_validation.md) for the evergreen Windows/macOS release checklist, expected UI fields by lifecycle state, staging commands, queue-depth checks, Stop/Start checks, and two-node round-robin evidence requirements.

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

During normal startup, desktop sidecars probe the active sidecar interpreter and, in GPU-capable modes, use platform-specific runtime bootstrap/repair where supported (Windows CUDA and macOS Metal) while preserving shared bridge lifecycle behavior. They emit:

- `desktop.runtime_setup ...` during sidecar start (backend selected + fallback reason)
- `compute_runtime ...` after `Llama(...)` init (backend actually used, offloaded
  layers, KV cache placement, and fallback reason)

Set `TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP=1` to explicitly disable runtime bootstrap and keep startup in probe-only mode (useful for packaging/troubleshooting while preserving fallback diagnostics).

When GPU runtime repair is needed, desktop uses the same interpreter binary that launches the sidecar process (`sys.executable`) and applies the repo-pinned `llama-cpp-python` source-build recipe with the platform flag:

- Windows CUDA: `CMAKE_ARGS=-DGGML_CUDA=on`
- macOS Metal: `CMAKE_ARGS=-DGGML_METAL=on`
- Both: `FORCE_CMAKE=1` and `pip install llama-cpp-python==<repo-pinned-version> --force-reinstall --no-cache-dir --verbose`

After a successful repair, the sidecar automatically re-execs once so the active process immediately uses the repaired runtime (no manual restart/environment flag required).

### Platform runtime expectations

- **macOS Apple Silicon**: validate with a Metal-enabled llama.cpp sidecar build and packaged `.app/Contents/Resources` resource resolution.
- **Windows 11 + NVIDIA GPU**: validate with a CUDA-enabled llama.cpp sidecar build.
- CPU fallback mode is available in both cases, with explicit fallback details surfaced in `desktop.runtime_setup ... fallback_reason=...` and `compute_runtime ... fallback_reason=...`. Missing `llama_cpp` is a dependency failure, not silent CPU fallback.
- `backend_available` reports runtime capability, `backend_selected` reports policy selection, and `backend_used` reports the initialized relay-processing runtime. GPU release claims depend on `backend_used`.

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
   - For Apple Silicon Macs (M1/M2/M3/M4), the release asset is intentionally
     named `token.place-desktop-<version>-apple-silicon.dmg` so users can pick
     the correct file quickly.
   - Desktop Tauri release staging is Tauri-only (`desktop-tauri/src-tauri/target/.../bundle`);
     legacy Electron artifacts from `desktop/` must never be published on this
     release channel.
   - If Apple signing credentials are not configured in CI, the workflow emits
     an explicit preview warning and uses ad-hoc signing for dev/preview builds.
     Those builds are not equivalent to fully Developer ID signed + notarized
     Gatekeeper-ready releases.
  - If signing credentials are configured, CI validates with `codesign`.
    Strict Gatekeeper notarization checks are skipped unless notarization is added.

### Unpaid macOS preview releases

- token.place can publish Apple Silicon preview DMGs without a paid
  Apple Developer Program account.
- These preview builds use ad-hoc signing and are not notarized, so Gatekeeper
  warnings after browser/GitHub download are expected.
- The mounted DMG now includes an inline opening guide (`README BEFORE OPENING.txt`)
  plus a sidecar release asset (`README-macos-apple-silicon-preview.txt`) that
  explain the same manual-open flow.
- Users must manually open/whitelist trusted previews:
  - Drag/copy `token.place desktop.app` to **Applications** and try opening once.
  - If macOS blocks with the expected “Apple could not verify…” dialog, click
    **Done**, then use **System Settings → Privacy & Security** to
    **Open Anyway / Allow / Open**.
  - Control-click (or right-click) and choose **Open** remains a fallback path.
- This flow does not remove Gatekeeper warnings for unpaid preview releases; it
  explains the expected manual allow/open steps.
- No-warning public distribution generally requires paid
  Developer ID signing plus notarization.

You can also run the workflow manually with `workflow_dispatch` and provide
`tag_name` to rebuild/re-publish an existing desktop tag.


The local prompt panel still uses `src-tauri/python/inference_sidecar.py` as a smoke test for local model setup.



## macOS packaged operator debug logs

Packaged desktop launches persist operator diagnostics so macOS users do not need a developer terminal to capture the same bridge/runtime detail that Windows console launches expose.

- Compute-node operator sessions write one log per session under the app log directory, in the `operators/` subdirectory. On macOS this is typically `~/Library/Logs/<bundle identifier>/operators/compute-node-<operator_session_id>.log`; use the exact **Debug log** path shown in the desktop UI because the bundle identifier can vary between preview/dev/release builds.
- Model bridge inspect/download diagnostics are appended to `model-bridge.log` in the same `operators/` log directory.
- The compute-node log mirrors safe bridge diagnostics: session start, selected bridge path, interpreter, resource root, import root/layout, `desktop_runtime_setup` probe/provision lines, compute-node stdout events, stderr from `compute_node_bridge.py`, runtime warm-load/model-manager lines, registration/poll/request/response metadata, and stop/cancel/unregister/cleanup lines.
- Logs must remain relay-blind: capture ciphertext/routing metadata and runtime diagnostics only; do not add plaintext prompt/response/tool/model-output logging.

Debug access is opt-in:

1. Start the operator, or reproduce a Start operator failure.
2. In the desktop **Compute node operator** panel, copy the **Debug log** path or use one of the debug buttons:
   - **Open debug log** opens the in-app read-only tail that can be refreshed.
   - **Reveal log file** opens Finder/Explorer/file manager at the current log file.
   - **Open debug terminal** opens Terminal.app on macOS and tails the current log file; paths with spaces are quoted by the Rust command path and are not executed from user input.
   - **Copy log tail** copies the in-app tail to the clipboard.
3. For validation sessions where a terminal should open automatically after the operator process is installed, launch the app with `TOKEN_PLACE_DESKTOP_OPEN_DEBUG_TERMINAL=1`.

When Start operator fails on macOS, include these lines in the bug report or release-validation note:

- `desktop.compute_node.session.start ... interpreter=... resource_root=... layout=... import_root=... log_file_path=...`
- `desktop.runtime_setup ... action=... interpreter=... prefix=... llama_module_path=... fallback_reason=...`
- Any `desktop.compute_node.stderr line=...` traceback/import/provisioning lines around `llama_cpp` or Metal.
- `desktop.compute_node_bridge.model_init.*`, `desktop.compute_node_bridge.registration.*`, and `desktop.compute_node_bridge.stop_requested` / unregister / cleanup lines if the bridge gets that far.

## Desktop logging defaults

Desktop subprocess logs now default to high-signal output:

- Known low-value llama.cpp noise (metadata dumps, control-token spam, per-layer assignment spam, tensor repack spam) is filtered at the Tauri stderr forwarding boundary.
- Warnings and errors are always preserved in default mode.
- The inference sidecar emits concise stderr summary lines for model init and prompt/eval throughput.

To restore full raw subprocess output (including verbose llama.cpp logs), run desktop with either:

- `TOKEN_PLACE_VERBOSE_LLM_LOGS=1`
- `TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS=1`

Both flags are equivalent opt-in toggles and intended for troubleshooting.


### Manual runtime verification

Use the helper below to print authoritative runtime wiring details from the same
Python environment used by desktop sidecars:

```bash
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf
```

The verifier exits non-zero if `llama_module_path` points at the repo-local shim
(`.../token.place/llama_cpp.py`). A healthy runtime should resolve
`llama_module_path` to the installed `llama-cpp-python` package path (for
example `.../site-packages/llama_cpp/__init__.py`).
When this shadowing is detected, the stable runtime action is
`shadowed_repo_llama_cpp` in both verifier and smoke-test diagnostics.

It prints:

- Shared runtime probe fields (`backend`, `gpu_offload_supported`,
  `detected_device`, `interpreter`, `prefix`, `llama_module_path`)
- `compute_runtime_*` summaries using stable fields
  (`requested`, `effective`, `backend_available`, `backend_used`,
  `device_backend`, `device_name`, `offloaded_layers`, `kv_cache`,
  `fallback_reason`, `interpreter`, `llama_module_path`)

### Regression and smoke tests

- Shared local desktop parity entry point (packaged resources + API v1 E2EE relay lifecycle):
  ```bash
  python desktop-tauri/scripts/run_desktop_parity_checks.py
  ```
- Operator startup regression coverage (bridge startup event + surfaced errors):
  ```bash
  pytest -q --noconftest tests/unit/test_desktop_compute_node_bridge.py
  npm --prefix desktop-tauri run test -- src/App.test.tsx
  ```
- Local Windows + NVIDIA GPU viability smoke test (same desktop Python/runtime path):
  ```bash
  python desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py --mode auto --model C:\\path\\to\\model.gguf
  ```
  Pass means desktop-side diagnostics and `compute_node_bridge.py` `started` events both report
  CUDA availability/usage with GPU offload and non-CPU KV cache. If the bridge exits before
  startup, errors will use the phrase `compute-node bridge exited before emitting a startup event`.
