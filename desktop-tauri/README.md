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
(`src-tauri/python/compute_node_bridge.py`) that reuses shared runtime helpers while driving the
API v1 relay-blind E2EE operator lifecycle: warm-load the relay-processing runtime, register to
the active relay URL, poll for ciphertext work, process locally, submit ciphertext responses, and
unregister on Stop when possible. The bridge runs as the primary operator path and emits status
events (running/registered, active relay URL, backend mode, model path, and last error).
Root `server.py` remains the canonical compute-node entrypoint; this desktop path must stay
parity-aligned with API v1 E2EE behavior and must not revive deprecated legacy relay endpoints as
production fallbacks.

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

During normal startup, desktop sidecars probe the active sidecar interpreter and, on
Windows in `auto`/`gpu`/`hybrid` modes, automatically run a one-time CUDA runtime
repair when the runtime is CPU-only. They emit:

- `desktop.runtime_setup ...` during sidecar start (backend selected + fallback reason)
- `compute_runtime ...` after `Llama(...)` init (backend actually used, offloaded
  layers, KV cache placement, and fallback reason)

Set `TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP=1` to explicitly disable the
Windows auto-repair path and keep startup in probe-only mode (useful for
packaging/troubleshooting while preserving normal CPU fallback diagnostics).

When Windows CUDA repair is needed, desktop uses the same interpreter binary that
launches the sidecar process (`sys.executable`) and applies the repo
source-build recipe:

- `CMAKE_ARGS=-DGGML_CUDA=on`
- `FORCE_CMAKE=1`
- `pip install llama-cpp-python==<repo-pinned-version> --force-reinstall --no-cache-dir --verbose`

After a successful repair, the sidecar automatically re-execs once so the active
process immediately uses the repaired runtime (no manual restart/environment flag required).

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

## Desktop parity release validation checklist

Use this checklist for every desktop operator release claim and for any change that touches the Tauri UI, Rust status/event bridge, packaged resources, Python bridge, runtime bootstrap, relay API v1 integration, or operator lifecycle. Windows and macOS should share one behavior checklist; platform-specific notes should explain runtime installation/probe differences only.

| Area | Windows CUDA expectation | macOS Metal expectation | CPU/fallback expectation | Where to validate |
| --- | --- | --- | --- | --- |
| GPU runtime | NVIDIA driver, CUDA toolkit/build tools, and a CUDA-capable `llama-cpp-python` are present for `auto`, `gpu`, or `hybrid` modes. | Apple Silicon or Metal-capable macOS has Xcode Command Line Tools and a Metal-capable `llama-cpp-python`. | Explicit `cpu` mode skips GPU bootstrap/probe requirements and reports CPU fields. | Local hardware + `verify_desktop_runtime.py`; CI can only cover mocked parity unless self-hosted GPU runners are added. |
| Dependency isolation | Desktop imports resolve from the desktop target/import root and site-packages, not user-site leakage or repo-local shims. | Same. Packaged `.app` resource layout must resolve the same bridge and import roots as dev. | Missing runtime/dependency fails closed before relay registration. | CI packaged inspect smoke and local `inspect` wrapper. |
| Packaged resource resolution | Packaged app resolves the Python bridge, requirements, runtime import root, and launcher used by dev flows. | Same for `.app` resources and Python launcher lookup. | Diagnostics include action, interpreter/import root, and next step without plaintext. | CI packaged bridge e2e. |
| Warm-load before register | Runtime warms before API v1 relay registration. | Same. | CPU mode still warms before registration. | CI/local relay parity e2e. |
| Relay registration | Register only after the active relay-processing runtime is ready for the active relay URL. | Same. | Fallback alone does not authorize registration; readiness does. | CI/local relay parity e2e; staging external node. |
| Multi-turn API v1 E2EE chat | Desktop processes multiple ciphertext requests and returns ciphertext responses without API v1 streaming. | Same. | CPU mode may be slower but behavior is identical. | CI/local relay parity e2e; staging client flow. |
| Stop | Stop cancels polling, unregisters when possible, emits stopped, and reports unregistered. | Same. | Same. | CI/local relay parity e2e and no-relay lifecycle e2e. |
| Start after Stop | New start gets a fresh session/sequence; stale events from old sessions do not overwrite active state. | Same. | Same. | CI/local relay parity e2e. |
| Two-node round-robin participation | A Windows node can be one of the two active relay participants without changing relay round-robin behavior. | A macOS node can be the other active participant with equivalent lifecycle semantics. | CPU nodes can participate only after warm readiness and should be labeled as CPU in diagnostics. | Staging-only with two external nodes; do not claim production round-robin until both platforms pass. |

### Lifecycle UI field expectations

| Lifecycle state | Running/status | Registration/polling | Runtime/backend fields | Model/work fields | Last error/action |
| --- | --- | --- | --- | --- | --- |
| idle | Operator stopped; no active session. | `registered=false`; no polling. | Requested mode may show saved preference; effective/backend fields are empty or `pending`. | No active request; queue depth may be unknown until a relay check runs. | Empty. |
| warming | Operator starting and model warm-load in progress. | `registered=false`; relay registration is blocked. | `backend_available` may be probe-derived; `backend_selected` shows the intended backend; `backend_used` remains `pending` until runtime init completes. | Model path/interpreter/import root may be shown; no request is processed. | Empty unless warm-load fails. |
| ready/registering | Warm-load complete; registration request in flight. | `registered=false` until relay acknowledges the active URL/session. | `backend_available`, `backend_selected`, and `backend_used` describe the warmed relay-processing runtime. | Ready for relay work; queue depth may still be unknown. | Empty or actionable registration error. |
| registered/polling | Operator running and registered. | `registered=true`; polling active against the active relay URL. | Backend fields remain stable for the active session. | Queue depth and last poll metadata may update; no plaintext prompts/responses are displayed. | Empty when healthy. |
| processing | Operator is handling a relay request. | Registration remains true unless the relay rejects/unregisters. | Backend fields continue to report the active runtime used for inference. | Active request id/safe metadata, byte counts, and timing may update; plaintext remains in memory only and is never logged. | Empty unless inference/submission fails. |
| stopped | User requested Stop or lifecycle completed shutdown. | `registered=false`; polling canceled; unregister attempted where possible. | Last known backend may remain visible as historical metadata; no active runtime work. | No active request; queue depth checks are manual/relay-derived. | Empty for clean stop; actionable if unregister failed. |
| failed | Startup, runtime, dependency, registration, polling, inference, or submission failed. | `registered=false` unless failure occurred after relay state became stale; next Start must create a fresh session. | Fields identify requested/selected/used backend as far as known plus fallback reason. | No new work should be accepted after fail-closed errors. | Actionable message with platform, action, interpreter/import root when relevant, and next step; no plaintext/ciphertext payload dumps. |

### Runtime field interpretation

- `backend_available` is the capability detected in the desktop Python environment, such as CUDA, Metal, CPU-only, or unavailable. It answers “what can this interpreter import and initialize?”
- `backend_selected` is the backend desktop intends to use after applying the user preference (`auto`, `gpu`/`hybrid`, `metal`, `cuda`, or `cpu`) and platform adapter decisions.
- `backend_used` is the backend actually used by the warmed relay-processing runtime. Release validation should trust this field over a stale probe when deciding whether CUDA/Metal parity passed.
- `fallback_reason` explains why selected and used backends differ, or why the runtime failed closed. Treat `user_requested_cpu`, `gpu_unavailable_cpu_fallback`, `probe_only`, `missing_llama_cpp`, `shadowed_repo_llama_cpp`, and install/bootstrap failures differently; only a warmed runtime with an acceptable reason may register.

### Manual validation commands

Run commands from the repository root unless noted.

```bash
# Local relay e2e parity: packaged resources, warm-load, register, multi-turn API v1 E2EE, Stop, Start after Stop.
desktop-tauri/scripts/validate_desktop_parity.sh local

# Equivalent Make target for local release checks.
make desktop-parity-checks

# Dependency-isolated packaged inspect smoke only.
TOKEN_PLACE_INSPECT_ONLY=1 python desktop-tauri/scripts/test_packaged_operator_e2e.py
# or
desktop-tauri/scripts/validate_desktop_parity.sh inspect

# Runtime/status check for the desktop Python environment.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf

# Windows CUDA smoke from PowerShell.
python desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py --mode auto --model C:\path\to\model.gguf

# macOS Metal runtime check.
CMAKE_ARGS="-DGGML_METAL=on" FORCE_CMAKE=1 python -m pip install llama-cpp-python --force-reinstall --no-cache-dir --verbose
python desktop-tauri/scripts/verify_desktop_runtime.py --mode metal --model /path/to/model.gguf

# Explicit CPU fallback check.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode cpu --model /path/to/model.gguf

# Desktop status and relay health checks.
curl -fsS http://127.0.0.1:5010/healthz
curl -fsS http://127.0.0.1:5010/relay/diagnostics
curl -fsS http://127.0.0.1:5010/metrics | head -n 40

# Queue-depth checks. Prefer metrics when available; diagnostics are safe metadata only.
curl -fsS http://127.0.0.1:5010/metrics | rg 'queue|knownServers|registered|poll'
curl -fsS http://127.0.0.1:5010/relay/diagnostics | python -m json.tool

# Stop/Start checks use the parity e2e by default; for manual UI validation, click Stop,
# confirm registered=false/polling stopped, click Start, and confirm a new session registers.
python desktop-tauri/scripts/test_desktop_relay_operator_parity_e2e.py
```

Staging-only validation requires a deployed relay plus real external compute nodes:

```bash
STAGING_RELAY=https://staging.token.place
curl -fsS "$STAGING_RELAY/livez"
curl -fsS "$STAGING_RELAY/healthz"
curl -fsS "$STAGING_RELAY/relay/diagnostics"
curl -fsS "$STAGING_RELAY/metrics" | head -n 80
# After one Windows CUDA node and one macOS Metal node register, verify knownServers >= 2
# and run an encrypted API v1 client flow that observes both nodes participating across turns.
```

### Do not fork platform behavior

- Put shared operator lifecycle behavior in the common Python bridge/runtime path first.
- Keep the Rust/Tauri status and event contract shared across Windows and macOS.
- Use platform adapters only for runtime installation, launcher discovery, packaging layout, and backend probe differences.
- Do not add Windows-only or macOS-only lifecycle state machines, registration rules, Stop/Start semantics, relay routes, or API v1 streaming behavior.
- Do not alter relay round-robin behavior to make a desktop validation pass; validation should reveal platform drift, not mask relay scheduling issues.
