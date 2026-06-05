# Desktop parity validation checklist

This checklist is the shared release and development gate for the token.place desktop operator on Windows and macOS. It is intentionally evergreen: use it for every desktop operator change and every release candidate that claims distributed desktop compute-node or two-node round-robin readiness, regardless of version number.

The architectural source of truth is [Desktop operator parity contract](architecture/desktop_operator_parity_contract.md). Keep one shared checklist and one shared implementation contract; do not let Windows and macOS accumulate separate lifecycle behavior.

## Parity principles: do not fork platform behavior

- Prefer shared Python bridge logic first. `compute_node_bridge.py`, `model_bridge.py`, runtime diagnostics, warm-load, API v1 registration, polling, Stop, and Start-after-Stop behavior should be cross-platform.
- Prefer the shared Rust status/event contract first. Tauri status fields and emitted events must mean the same thing on Windows and macOS.
- Use platform adapters only where the platform truly differs: runtime install/probe commands, CUDA versus Metal build flags, Python launcher discovery, packaged resource paths, and signing/notarization flows.
- Keep relay-blind API v1 E2EE invariant: relay-owned state, logs, diagnostics, and payloads may contain ciphertext and safe routing metadata only. Do not add plaintext prompt, response, tool argument, or model-output logging while debugging desktop parity.
- API v1 relay inference is non-streaming. Do not add streaming or legacy `/sink`, `/faucet`, `/source`, `/retrieve`, or `/next_server` fallbacks to make desktop parity pass.

## Required parity checklist

| Area | Windows CUDA expectation | macOS Metal expectation | CPU fallback expectation | Evidence to capture |
| --- | --- | --- | --- | --- |
| GPU runtime availability | CUDA-capable `llama-cpp-python` is installed or repaired for `auto`/`cuda`/GPU modes. | Metal-capable `llama-cpp-python` is installed or repaired for `auto`/`metal`/GPU modes. | Explicit `cpu` mode skips GPU bootstrap and reports CPU cleanly. | `verify_desktop_runtime.py` output and bridge `started` event fields. |
| Dependency isolation | Desktop imports resolve from the desktop target/import root and never from user site packages or repo-local shims. | Same isolation inside `.app/Contents/Resources`. | Same isolation for CPU-only installs. | Packaged bridge e2e, `llama_module_path`, `interpreter`, `prefix`, and import-root diagnostics. |
| Packaged resource resolution | Packaged app resolves the shared bridge, model bridge, runtime setup, requirements, and config files. | `.app/Contents/Resources` resolves the same shared files and launcher paths. | Same paths remain valid without GPU libraries. | `test_packaged_operator_e2e.py` logs. |
| Warm-load before register | Bridge warms the relay-processing runtime before API v1 registration. | Same sequence. | Same sequence. | Bridge logs contain `model_init.ready` with `reason=pre_registration` before `api_v1_e2ee.register`. |
| Relay registration | Registered only after warm-load and runtime readiness. | Same behavior. | Same behavior when CPU runtime is explicitly ready. | `/relay/diagnostics` and bridge status show `registered: true`. |
| Multi-turn API v1 E2EE chat | Multiple encrypted `/api/v1/chat/completions` turns complete without plaintext relay state. | Same behavior. | Same behavior. | Client decrypts responses; relay logs show request queued, response received, response retrieved. |
| Stop | Stop cancels polling, unregisters when possible, exits bridge, and reports `registered: false`. | Same behavior. | Same behavior. | Desktop status and `/relay/diagnostics` show no stale node. |
| Start after Stop | A fresh session starts, warms, registers, and answers another encrypted turn; stale old-session events do not overwrite status. | Same behavior. | Same behavior. | New `operator_session_id` and successful post-restart turn. |
| Two-node round-robin participation | Two Windows-capable nodes can register and participate according to relay scheduling. | Two macOS-capable nodes can register and participate according to relay scheduling. | CPU-only nodes participate only when explicitly accepted for the release claim. | Staging diagnostics show two nodes, queue depth returns to zero, and per-node assignment/response logs demonstrate rotation. |

Do not make production two-node or round-robin claims until both Windows and macOS release candidates pass this checklist, including GPU-capable runtime evidence for the platform-specific release artifacts.

## Expected desktop UI fields by lifecycle state

| Lifecycle state | Button/operator state | Relay/runtime status fields | Backend fields | Error fields |
| --- | --- | --- | --- | --- |
| `idle` | Start enabled when required inputs are valid; Stop disabled. | `running: false`, `registered: false`, no active relay polling, no active `operator_session_id`. | Requested mode may show the user preference; effective/backend fields may be empty or last-known but must not claim active registration. | `last_error` empty/null. |
| `warming` | Start disabled; Stop enabled. | `running: true`, `registered: false`, `relay_runtime_state: starting` or `warming`, warm-load enabled when configured. | `backend_available`, `backend_selected`, and `backend_used` stay `pending` until the relay-processing runtime is actually ready. | Empty unless warm-load/runtime setup fails. |
| `ready/registering` | Start disabled; Stop enabled. | `running: true`, warm-load complete, registration request in flight or about to be sent. | Fields describe the warmed relay-processing runtime. | Empty unless registration fails; failed registration must include safe routing diagnostics only. |
| `registered/polling` | Start disabled; Stop enabled. | `running: true`, `registered: true`, `relay_runtime_state: ready`, active relay URL, current session id. | `backend_available`, `backend_selected`, and `backend_used` are concrete (`cuda`, `metal`, or `cpu`). | Empty/null. |
| `processing` | Start disabled; Stop enabled. | `running: true`, `registered: true`, `relay_runtime_state: processing`; queue depth should drain after each turn. | Same concrete backend fields as ready; no stale probe-only fields. | Empty/null unless inference fails closed. |
| `stopped` | Start enabled when inputs remain valid; Stop disabled. | `running: false`, `registered: false`, relay polling stopped, previous node unregistered when possible. | Last-known fields may remain visible for diagnostics but must not imply active registration. | Empty/null for clean Stop. |
| `failed` | Start enabled if the app can retry; Stop disabled unless a child process is still being cleaned up. | `running: false` or cleanup-in-progress, `registered: false`, `relay_runtime_state: failed`. | Missing dependencies report `backend_available: unavailable` and `backend_selected/backend_used: pending`; real CPU fallback reports `cpu` only after a usable CPU runtime is warmed. | Concise, actionable, platform-specific `last_error`; no plaintext payloads or secrets. |

## Runtime backend guidance

### Windows CUDA prerequisites

- NVIDIA GPU and compatible driver visible to Windows.
- Build tools required by the pinned `llama-cpp-python` source build.
- CUDA-enabled install/repair uses `CMAKE_ARGS=-DGGML_CUDA=on`, `FORCE_CMAKE=1`, and the repo-pinned `llama-cpp-python` version.
- Validate with `desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py` on real Windows NVIDIA hardware before claiming CUDA release readiness.

### macOS Metal prerequisites

- Apple Silicon Mac or another Mac that can run a Metal-capable llama.cpp backend.
- Xcode Command Line Tools available for local source builds when a wheel is not sufficient.
- Metal-enabled install/repair uses `CMAKE_ARGS=-DGGML_METAL=on`, `FORCE_CMAKE=1`, and the repo-pinned `llama-cpp-python` version.
- Validate the packaged `.app` path as well as the development path so `.app/Contents/Resources` uses the same bridge/runtime code as Windows packaged builds.

### Backend field meanings

- `backend_available`: what the runtime probe found in the active desktop Python environment (`cuda`, `metal`, `cpu`, `unavailable`, or similar). This is capability, not proof that a relay turn used it.
- `backend_selected`: what desktop selected after applying the user mode (`auto`, `cuda`, `metal`, `cpu`) and bootstrap/fallback policy. This should stay `pending` until selection is known.
- `backend_used`: what the warmed relay-processing runtime actually used after initialization. This is the release-critical field for GPU claims.
- `fallback_reason`: why selected/used backend differs from the preferred GPU path. A healthy GPU path should have an empty/null reason. CPU mode should say CPU was explicitly selected. Missing dependencies are failures, not silent CPU fallback.

## Validation command matrix

Use these commands as copy-paste starting points. Replace URLs, tokens, model paths, and release tags with environment-specific values.

### Local-only relay e2e

```bash
# Run the single shared desktop parity entry point against a local relay with mock LLM.
python desktop-tauri/scripts/run_desktop_parity_checks.py

# Or run the underlying local relay parity test directly.
python desktop-tauri/scripts/test_desktop_relay_operator_parity_e2e.py

# Inspect local relay health and diagnostics while the test or app is running.
curl -fsS http://127.0.0.1:5010/livez
curl -fsS http://127.0.0.1:5010/relay/diagnostics | python -m json.tool
```

### CI-only checks

```bash
# Linux Tauri UI and packaged bridge smoke tests require CI/webkit/xvfb setup.
xvfb-run -a python desktop-tauri/scripts/test_desktop_operator_ui_e2e.py
python desktop-tauri/scripts/test_packaged_operator_e2e.py

# Windows/macOS packaged parity jobs are defined in GitHub Actions.
gh workflow run desktop-operator-e2e.yml
```

### Local hardware runtime checks

```powershell
# Windows PowerShell on NVIDIA hardware.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model C:\path\to\model.gguf
python desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py --mode auto --model C:\path\to\model.gguf
```

```bash
# macOS shell on Metal-capable hardware.
CMAKE_ARGS=-DGGML_METAL=on FORCE_CMAKE=1 python -m pip install --force-reinstall --no-cache-dir llama-cpp-python
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf
python desktop-tauri/scripts/test_desktop_relay_operator_parity_e2e.py
```

```bash
# Explicit CPU fallback check on either platform.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode cpu --model /path/to/model.gguf
```

### Staging-only relay validation

```bash
# Basic staging relay health.
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/relay/diagnostics | python -m json.tool

# Start two desktop operators from the release candidates, both pointed at staging.
# In each desktop app: set Relay URL to https://staging.token.place, choose the intended backend mode, then click Start operator.

# Confirm two registered nodes before making two-node/round-robin claims.
curl -fsS https://staging.token.place/relay/diagnostics \
  | python -c "import json,sys; d=json.load(sys.stdin); nodes=d.get('registered_compute_nodes', []); print('nodes=', len(nodes)); print(json.dumps(nodes, indent=2))"
```

### Desktop status checks

```bash
# Runtime wiring from the active desktop Python environment.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf

# Relay diagnostics should show safe metadata only: ids, queue depth, registration age, and routing state.
curl -fsS http://127.0.0.1:5010/relay/diagnostics | python -m json.tool
curl -fsS https://staging.token.place/relay/diagnostics | python -m json.tool
```

### Queue-depth checks

```bash
# Local queue depth snapshot.
curl -fsS http://127.0.0.1:5010/relay/diagnostics \
  | python -c "import json,sys; d=json.load(sys.stdin); print([(n.get('server_id'), n.get('queue_depth')) for n in d.get('registered_compute_nodes', [])])"

# Staging queue depth snapshot; expect all depths to return to 0 after each completed turn.
curl -fsS https://staging.token.place/relay/diagnostics \
  | python -c "import json,sys; d=json.load(sys.stdin); print([(n.get('server_id'), n.get('queue_depth')) for n in d.get('registered_compute_nodes', [])])"
```

### Stop/Start checks

```bash
# Local: after clicking Stop operator, diagnostics should have no stale node for that session.
curl -fsS http://127.0.0.1:5010/relay/diagnostics | python -m json.tool

# Staging: after clicking Stop operator, registered node count should decrease; after Start, it should increase with a fresh session.
curl -fsS https://staging.token.place/relay/diagnostics \
  | python -c "import json,sys; d=json.load(sys.stdin); print('nodes=', len(d.get('registered_compute_nodes', [])))"
```

## Release sign-off

Before release sign-off or production round-robin messaging, record:

1. Windows CUDA runtime verifier or smoke-test output.
2. macOS Metal runtime verifier output from the packaged app path or release-candidate environment.
3. CPU fallback verifier output.
4. Local relay parity e2e result.
5. Staging relay health, diagnostics, queue-depth drain, Stop, and Start-after-Stop evidence.
6. Two-node staging participation evidence for both Windows and macOS release candidates.

If any item is unavailable because CI, staging, GPU hardware, signing, or network access is missing, mark it as a release blocker or an explicitly accepted preview limitation. Do not silently downgrade platform parity expectations.

## macOS packaged operator debug logs

Packaged desktop operator sessions persist a per-session debug log in the app log directory. On macOS this is under:

```text
~/Library/Logs/place.token.desktop/operator/compute-node-operator-<operator_session_id>.log
```

The exact path is shown in the desktop UI as **Debug log** after an operator start creates the session. The log mirrors the operator bridge lines that are otherwise visible in a developer terminal on Windows, including:

- `desktop.compute_node.session.start` with the sanitized relay target, `operator_session_id`, selected bridge, interpreter, resource root, layout, import root, and debug log path.
- `desktop.compute_node.stderr line=...` output from `compute_node_bridge.py`, including `desktop_runtime_setup` probe/provision diagnostics.
- `desktop.compute_node.stdout line=...` bridge status events for warm-load, registration, polling, request/response lifecycle metadata, cancel, unregister, and cleanup.
- `desktop.compute_node.bridge_process_exited` and stop/cancel/kill diagnostics.

The log is intended for safe operational metadata. Do not add plaintext prompts, model responses, tool arguments, private keys, or decrypted relay payloads to these logs. Public key fingerprints and sanitized relay hostnames are acceptable.

### Opening the log on macOS

Use one of the opt-in desktop controls in the **Compute node operator** panel:

- **Open debug log** opens the current persisted log file.
- **Reveal log file** shows it in Finder.
- **Open debug terminal** opens Terminal.app tailing the current log with a read-only `tail -n +1 -F` command.
- **Copy log path** copies the exact path for bug reports.
- **Operator debug console** shows live in-app log lines for users who do not want Terminal.app to open.

For manual validation of packaged macOS builds, launch the app with this environment variable to automatically open the debug terminal when the operator session starts:

```bash
TOKEN_PLACE_DESKTOP_OPEN_DEBUG_TERMINAL=1 open -a "token.place desktop"
```

If **Start operator** fails, capture the **Last error** field plus the debug log lines around `desktop.compute_node.session.start`, `desktop_runtime_setup`, `llama_module_path`, `interpreter`, `prefix`, `registration`, and `bridge_process_exited`. Stop the operator before collecting final logs so cancel/unregister/cleanup lines are included.
