# Desktop Windows/macOS parity validation

Desktop operator behavior is a shared contract across Windows and macOS. Treat this page as the
evergreen development and release checklist for desktop compute-node changes; do not fork it into
separate platform-specific checklists that drift over time.

## Validation scope labels

| Label | Where it runs | What it proves | Typical command |
| --- | --- | --- | --- |
| CI-only | GitHub-hosted runners without CUDA/Metal hardware or real GGUF models | Packaged import/resource resolution, dependency isolation, mocked API v1 E2EE relay lifecycle, Stop, and Start after Stop | `python desktop-tauri/scripts/run_desktop_parity_checks.py` |
| Local-only | Developer or release hardware with real Windows CUDA or macOS Metal runtime | GPU-capable `llama-cpp-python` install, runtime diagnostics, and CPU fallback interpretation | `python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf` |
| Staging-only | External relay such as `https://staging.token.place` plus real desktop nodes | Registration visibility, queue depth, multi-node participation, and production-like relay health | `curl -fsS "$STAGING_RELAY/relay/diagnostics"` |

## Shared desktop parity checklist

Every desktop release candidate and any change that touches Python bridge logic, Tauri status/event
contracts, packaged resources, or relay participation must validate this checklist against both
Windows and macOS before making two-node or round-robin production claims:

- [ ] **Windows CUDA:** on Windows 11 with an NVIDIA GPU, `auto`, `gpu`, or `hybrid` mode detects a
  CUDA-capable runtime and reports GPU usage instead of silently falling back to CPU.
- [ ] **macOS Metal:** on Apple Silicon, `auto`, `gpu`, or `hybrid` mode detects a Metal-capable
  runtime and reports GPU usage instead of silently falling back to CPU.
- [ ] **CPU fallback:** explicit `cpu` mode works on both platforms, and any automatic fallback has a
  clear `fallback_reason` rather than ambiguous `unknown` or `pending` fields after readiness.
- [ ] **Dependency isolation:** packaged bridge imports resolve from bundled resources or the active
  sidecar interpreter, never from repo-local shadows such as `llama_cpp.py`.
- [ ] **Packaged resource resolution:** Windows-style resources and macOS `.app/Contents/Resources`
  layouts resolve the same bridge/runtime files.
- [ ] **Warm-load before register:** the model runtime reaches ready state before the operator reports
  relay registration.
- [ ] **Relay registration:** `/relay/diagnostics` shows the external compute node and the desktop UI
  reports `Registered: yes` only when the relay-processing runtime is ready.
- [ ] **Multi-turn API v1 E2EE chat:** at least three encrypted API v1 turns complete through the
  relay without plaintext relay-owned state, logs, or diagnostics.
- [ ] **Stop:** Stop unregisters the compute node and drains the relay diagnostics count back to zero.
- [ ] **Start after Stop:** starting again after Stop creates a fresh session and can process another
  encrypted API v1 turn.
- [ ] **Two-node round-robin participation:** with one Windows node and one macOS node registered to
  the same staging relay, repeated API v1 encrypted requests select both nodes in registration-order
  round-robin. Do not change relay round-robin behavior as part of desktop parity work.

## Single script entry point

Use the shared entry point for local and CI desktop parity checks:

```bash
python desktop-tauri/scripts/run_desktop_parity_checks.py
```

The default profile runs:

1. `test_packaged_operator_e2e.py` for dependency-isolated packaged bridge resolution.
2. `test_desktop_relay_operator_parity_e2e.py` for local mocked relay registration, warm-load,
   multi-turn API v1 E2EE chat, Stop, Start after Stop, and queue-depth drain checks.

Optional local hardware probes:

```bash
# macOS Apple Silicon or Windows CUDA: inspect the active desktop runtime and diagnostics.
python desktop-tauri/scripts/run_desktop_parity_checks.py --model /path/to/model.gguf --mode auto

# Windows NVIDIA release hardware: add the CUDA smoke assertion.
python desktop-tauri/scripts/run_desktop_parity_checks.py --model C:\path\to\model.gguf --mode auto --gpu-smoke

# macOS built-app lifecycle check after building the Tauri binary/app bundle.
python desktop-tauri/scripts/run_desktop_parity_checks.py --include-macos-no-relay
```

CI still invokes individual scripts where a workflow needs a narrower matrix leg, but those commands
must stay equivalent to the shared entry point rather than defining a different platform contract.

## Manual validation commands

### Local relay e2e

Run the self-contained mocked local relay parity harness:

```bash
python desktop-tauri/scripts/run_desktop_parity_checks.py
```

For focused debugging, run the underlying relay parity script directly:

```bash
python desktop-tauri/scripts/test_desktop_relay_operator_parity_e2e.py
```

Expected result: the script exits `0`, log files under `.desktop-e2e-logs/` show API v1 encrypted
request queue/response events, and `/relay/diagnostics` returns zero registered nodes after Stop.

### Staging relay validation

Use an environment variable so the same commands can target staging, production preview, or a local
relay without rewriting examples:

```bash
export STAGING_RELAY=https://staging.token.place
curl -fsS "$STAGING_RELAY/healthz"
curl -fsS "$STAGING_RELAY/relay/diagnostics"
curl -fsS "$STAGING_RELAY/metrics" | head -n 40
```

With one Windows node and one macOS node registered, confirm both nodes are visible:

```bash
curl -fsS "$STAGING_RELAY/relay/diagnostics" | python -m json.tool
```

Expected result: `total_registered_compute_nodes` is `2`, each entry in `registered_compute_nodes`
has a `queue_depth`, and no diagnostics value contains plaintext prompts, messages, responses, tool
arguments, or model output.

Confirm API v1 selection reaches both registered nodes without changing relay round-robin behavior:

```bash
export STAGING_RELAY=https://staging.token.place
for i in 1 2 3 4; do
  curl -fsS "$STAGING_RELAY/api/v1/relay/servers/next" | python -c 'import json,sys,hashlib; d=json.load(sys.stdin); k=d.get("server_public_key", ""); print(hashlib.sha256(k.encode()).hexdigest()[:12])'
done
```

Expected result: with exactly two API v1-capable desktop nodes registered, the printed fingerprints
alternate between two values in registration-order round-robin.

### Desktop status checks

From the desktop UI, verify these fields after pressing **Start operator**:

```text
Running
Registered
Relay runtime state
Runtime path
Relay runtime path
Active relay URL
Requested mode
Effective mode
Backend available
Backend selected
Backend used
Fallback reason
Model path
Last error
```

For runtime details from the same Python environment used by sidecars:

```bash
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf
```

### Queue-depth checks

Check staging queue depth without exposing payload content:

```bash
export STAGING_RELAY=https://staging.token.place
curl -fsS "$STAGING_RELAY/relay/diagnostics" \
  | python -c 'import json,sys; d=json.load(sys.stdin); print([(i, n.get("queue_depth")) for i,n in enumerate(d.get("registered_compute_nodes", []), 1)])'
```

During idle/after Stop, expected queue depths are `0`. During processing, a node may briefly show a
positive queue depth, but it must return to `0` after the client retrieves the encrypted response.

### Stop/Start checks

Use the desktop UI for manual release validation:

1. Start the operator on Windows and macOS.
2. Confirm both nodes appear in relay diagnostics.
3. Send at least three encrypted API v1 chat turns through the staging relay.
4. Press **Stop operator** on one node.
5. Confirm diagnostics decrement and that node's UI reports stopped state.
6. Press **Start operator** on the same node.
7. Confirm diagnostics increment again and another encrypted API v1 turn completes.

Copy-paste diagnostics around each step:

```bash
export STAGING_RELAY=https://staging.token.place
watch -n 2 'curl -fsS "$STAGING_RELAY/relay/diagnostics" | python -m json.tool'
```

If `watch` is unavailable, rerun this portable command manually:

```bash
curl -fsS "$STAGING_RELAY/relay/diagnostics" | python -m json.tool
```

## Expected UI fields by lifecycle state

| Lifecycle state | Running | Registered | Relay runtime state | Backend fields | Fallback reason | Last error | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| idle | `no` | `no` | `idle` or blank before first status load | `pending`, blank, or saved defaults | `none` | `none` | No bridge process is running. |
| warming | `yes` | `no` | `starting` then `warming` | `pending` until runtime readiness | `none` | `none` unless warm-load fails | Registration must not be reported yet. |
| ready/registering | `yes` | `no` until relay accepts registration | `ready` | concrete values such as `cuda`, `metal`, or `cpu` | `none` or explicit fallback detail | `none` | Runtime is warm and relay registration may start. |
| registered/polling | `yes` | `yes` | `ready` | concrete values | `none` or explicit fallback detail | `none` | Node is eligible for API v1 E2EE relay work. |
| processing | `yes` | `yes` | `processing` or `ready` between polls | concrete values | unchanged from ready state | `none` unless request handling fails | Queue depth may be briefly positive. |
| stopped | `no` | `no` | `stopped` | last concrete values may remain visible | unchanged from last run | `none` for graceful Stop | Relay diagnostics should no longer list the node. |
| failed | `no` | `no` | `failed` | `pending` or last known values | required when fallback caused failure | error message required | GPU-only failure should fail closed rather than silently use CPU. |

## Runtime guidance

### Windows CUDA prerequisites

- Windows 11 or compatible Windows host with an NVIDIA GPU and current NVIDIA driver.
- Visual Studio Build Tools with C++ core features, C++ CMake tools, and a Windows SDK.
- CUDA Toolkit installed and visible to the build environment.
- A CUDA-enabled `llama-cpp-python` build for the interpreter that launches the desktop sidecar:

```powershell
$env:CMAKE_ARGS = "-DGGML_CUDA=on"
$env:FORCE_CMAKE = "1"
python -m pip install llama-cpp-python --force-reinstall --no-cache-dir --verbose
```

### macOS Metal prerequisites

- Apple Silicon macOS host.
- Xcode Command Line Tools and CMake.
- A Metal-enabled `llama-cpp-python` build for the interpreter that launches the desktop sidecar:

```bash
xcode-select --install  # if command line tools are not installed yet
brew install cmake
CMAKE_ARGS="-DGGML_METAL=on" FORCE_CMAKE=1 python -m pip install llama-cpp-python --force-reinstall --no-cache-dir --verbose
```

### Diagnostic field meanings

- `backend_available`: what the runtime probe found on this host before model initialization, such
  as `cuda`, `metal`, or `cpu`.
- `backend_selected`: what desktop chose from the requested mode and available backend. For example,
  `auto` on Apple Silicon should select `metal` when Metal is available.
- `backend_used`: what `llama-cpp-python` actually used after `Llama(...)` initialization. This is
  the authoritative release-readiness field for GPU usage.
- `fallback_reason`: why selected/used backend fell back or failed. `none` is expected only when the
  requested backend is available and used, or when explicit CPU mode was requested.

Interpretation rules:

- `backend_selected=metal` or `cuda` with `backend_used=cpu` is a fallback. It is acceptable only in
  CPU fallback validation and must include a useful `fallback_reason`.
- `gpu` mode on Windows/macOS should fail closed if the GPU runtime is unavailable; do not report
  production GPU readiness from CPU fallback results.
- `pending` values are valid only before relay runtime readiness. They are not acceptable for a
  registered/polling release sign-off state.

## Do not fork platform behavior

- Implement shared Python bridge behavior first. Windows and macOS should call the same bridge
  lifecycle, warm-load, registration, API v1 E2EE, Stop, and Start-after-Stop code paths.
- Keep the Rust/Tauri status and event contract shared. UI fields should mean the same thing on both
  platforms.
- Limit platform adapters to runtime installation/probe differences, packaged path discovery, and
  OS-specific launcher mechanics.
- Do not add Windows-only or macOS-only relay lifecycle semantics. If a platform needs special
  handling, express it as a small adapter feeding the shared contract.
- Do not route around API v1 E2EE or revive deprecated legacy relay endpoints for parity shortcuts.
