# Desktop parity validation checklist

Desktop operator parity is an evergreen release requirement for token.place. A
Windows desktop operator and a macOS desktop operator must expose the same user
lifecycle, relay lifecycle, status fields, and relay-blind API v1 E2EE behavior.
Platform-specific code is allowed only where the runtime must be installed or
probed differently.

## Parity contract

For every desktop release candidate and every desktop runtime/bridge change,
validate the same checklist on Windows and macOS before claiming two-node or
round-robin production readiness.

| Area | Windows expectation | macOS expectation | Required evidence |
| --- | --- | --- | --- |
| GPU runtime | CUDA-capable `llama-cpp-python` build on NVIDIA machines. | Metal-capable `llama-cpp-python` build on Apple Silicon and supported Intel Macs. | Runtime diagnostics show GPU backend availability and usage for GPU/hybrid modes. |
| CPU fallback | CPU mode works without CUDA and reports why GPU was not used. | CPU mode works without Metal and reports why GPU was not used. | `fallback_reason` is explicit when requested GPU/hybrid mode cannot use GPU. |
| Dependency isolation | Packaged bridge imports from packaged resources/site packages, not repo-local shims. | Packaged `.app` imports from packaged resources/site packages, not repo-local shims. | Packaged inspect smoke passes and `llama_module_path` is not `./llama_cpp.py`. |
| Packaged resource resolution | The packaged layout resolves the same Python bridge/runtime files as development. | The `.app` resource layout resolves the same Python bridge/runtime files as development. | Packaged operator bridge e2e passes. |
| Warm-load before register | Model/runtime warm-load completes before relay registration. | Model/runtime warm-load completes before relay registration. | Logs include `desktop.compute_node_bridge.model_init.ready reason=pre_registration` before register. |
| Relay registration | Operator registers through API v1 E2EE compute-node routes. | Operator registers through API v1 E2EE compute-node routes. | `/relay/diagnostics` lists the node using safe routing metadata only. |
| Multi-turn relay chat | Multiple API v1 E2EE chat turns complete without plaintext relay state/logs. | Multiple API v1 E2EE chat turns complete without plaintext relay state/logs. | Local or staging API v1 relay parity e2e completes two or more turns. |
| Stop | Stop terminates polling/processing and clears registered UI state. | Stop terminates polling/processing and clears registered UI state. | UI shows `Running: no`, `Registered: no`, and stopped state. |
| Start after Stop | A fresh Start creates a new session and can register again. | A fresh Start creates a new session and can register again. | Start/Stop/Start e2e or manual UI validation passes. |
| Two-node round-robin | A Windows node can participate with a second node without starving it. | A macOS node can participate with a second node without starving it. | Staging diagnostics/metrics show both nodes receive work over repeated encrypted requests. |

## Do not fork platform behavior

- Put shared operator behavior in the Python bridge/runtime path first. Windows
  and macOS should not have separate relay lifecycles, prompt handling,
  registration timing, Stop behavior, or Start-after-Stop behavior.
- Keep the Rust status/event contract shared. UI fields should be populated from
  the same status event keys on every platform.
- Use platform adapters only for runtime install/probe differences, such as
  CUDA repair/build steps on Windows or Metal build/probe steps on macOS.
- API v1 relay traffic remains non-streaming and relay-blind E2EE. Relay-owned
  state, logs, diagnostics, and metrics may contain ciphertext plus safe routing
  metadata only; plaintext prompts, responses, tool arguments, or model output
  must fail closed instead of being queued or logged.

## Validation tiers

| Tier | Scope | Commands | Notes |
| --- | --- | --- | --- |
| CI-only | Linux UI smoke, Windows packaged/API v1 parity, macOS packaged/API v1 parity, macOS no-relay lifecycle. | GitHub Actions workflow `Desktop operator app e2e`. | CI runners do not prove release-class CUDA or Metal acceleration. |
| Local-only | Hardware runtime checks, app UI lifecycle, CPU fallback on developer machines. | `verify_desktop_runtime.py`, GPU smoke scripts, `npm run tauri dev`. | Run on the actual release hardware class whenever GPU claims change. |
| Staging-only | External relay registration, queue depth, multi-turn encrypted chat, and two-node round-robin. | `curl`/Python probes against `https://staging.token.place`. | Requires staging relay access and at least one real external desktop compute node. |

## Shared script entry point

Use one entry point for packaged bridge and API v1 relay parity checks so the
Windows/macOS checklist stays aligned:

```sh
# Cross-platform CPU/local relay parity smoke.
python desktop-tauri/scripts/run_desktop_parity_checks.py --profile local-cpu

# Same command via Makefile.
make desktop-parity-check

# CI-equivalent Windows packaged/API v1 relay parity.
python desktop-tauri/scripts/run_desktop_parity_checks.py --profile ci-windows

# CI-equivalent macOS packaged/API v1 relay parity plus no-relay lifecycle.
python desktop-tauri/scripts/run_desktop_parity_checks.py --profile ci-macos
```

The wrapper intentionally delegates to existing focused scripts; it does not
replace hardware validation or staging validation.

## Copy-paste manual validation commands

### Local relay e2e

```sh
# Install focused verification dependencies when needed.
python -m pip install -r config/requirements_codex_verification.txt

# Run the shared packaged-resource + local API v1 relay parity checks.
python desktop-tauri/scripts/run_desktop_parity_checks.py --profile local-cpu
```

### Desktop runtime status checks

```sh
# CPU fallback/status check; should be valid on Windows and macOS.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode cpu --model /path/to/model.gguf

# Windows CUDA status check on a Windows NVIDIA release-validation host.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode gpu --model C:\path\to\model.gguf
python desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py --mode auto --model C:\path\to\model.gguf

# macOS Metal status check on a macOS release-validation host.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode gpu --model /path/to/model.gguf
```

### Desktop UI Stop/Start checks

```sh
cd desktop-tauri
npm ci
npm run tauri dev
```

Then use the UI to select a GGUF, set the relay URL, click **Start operator**,
wait for warm-load/registration, click **Stop operator**, and click **Start
operator** again. The expected fields by lifecycle state are in the table below.

### Staging relay validation

```sh
export TOKENPLACE_STAGING_RELAY=https://staging.token.place

curl -fsS "$TOKENPLACE_STAGING_RELAY/healthz"
curl -fsS "$TOKENPLACE_STAGING_RELAY/livez"
curl -fsS "$TOKENPLACE_STAGING_RELAY/relay/diagnostics"
curl -fsS "$TOKENPLACE_STAGING_RELAY/metrics" | head -n 80
```

After starting the desktop operator against the staging relay, repeat the
safe-metadata checks:

```sh
curl -fsS "$TOKENPLACE_STAGING_RELAY/relay/diagnostics" | python -m json.tool
curl -fsS "$TOKENPLACE_STAGING_RELAY/metrics" | grep -E 'queue|registered|poll|request|response|round|server' | head -n 80
```

### Queue-depth checks

```sh
# Local relay queue/registration safe-metadata check.
curl -fsS http://127.0.0.1:5010/relay/diagnostics | python -m json.tool
curl -fsS http://127.0.0.1:5010/metrics | grep -E 'queue|registered|poll|request|response|round|server' | head -n 80

# Staging relay queue/registration safe-metadata check.
curl -fsS "$TOKENPLACE_STAGING_RELAY/relay/diagnostics" | python -m json.tool
curl -fsS "$TOKENPLACE_STAGING_RELAY/metrics" | grep -E 'queue|registered|poll|request|response|round|server' | head -n 80
```

### Stop/Start relay checks

```sh
# 1. With the desktop operator running, confirm the node appears in diagnostics.
curl -fsS "$TOKENPLACE_STAGING_RELAY/relay/diagnostics" | python -m json.tool

# 2. Click Stop operator in the desktop UI, then confirm the node no longer polls/accepts work.
curl -fsS "$TOKENPLACE_STAGING_RELAY/relay/diagnostics" | python -m json.tool

# 3. Click Start operator again, then confirm a fresh registration/session appears.
curl -fsS "$TOKENPLACE_STAGING_RELAY/relay/diagnostics" | python -m json.tool
```

## Expected desktop UI fields by lifecycle state

| Lifecycle state | Running | Registered | Relay runtime state | Runtime/relay runtime path | Backend fields | Fallback reason | Last error | Primary operator action |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| idle | `no` | `no` | `idle` | `pending` until loaded | Requested mode may show config; selected/used pending | `none` | `none` | **Start operator** enabled when model path is set. |
| warming | `yes` or starting transition | `no` | `starting` or `warming` | Runtime path should become `bridge`; relay runtime path should become `bridge` when API v1 warm-load is active. | `backend_available`/`backend_selected` may still be pending until probe/init completes. | `none` unless a GPU request already fell back. | `none` | **Stop operator** enabled once process is running. |
| ready/registering | `yes` | `no` until relay accepts registration | `ready` or `registering` | Both paths should be populated. | Available and selected backend should be populated; used backend may still be pending until model init reports. | Explicit when selected backend differs from requested mode. | `none` | Wait for registration; do not show registered `yes` during warm-load. |
| registered/polling | `yes` | `yes` | `registered` or `polling` | Populated. | `backend_available`, `backend_selected`, and `backend_used` populated. | `none` for expected GPU/CPU usage; explicit for fallback. | `none` | **Stop operator** available. |
| processing | `yes` | `yes` | `processing` | Populated. | Backend fields remain stable for the active session. | Unchanged unless runtime reports a fallback. | `none` unless the request fails. | Stop should cancel/terminate cleanly without exposing plaintext relay state. |
| stopped | `no` | `no` | `stopped` | Last known paths may remain visible for diagnostics. | Last known backend fields may remain visible. | Last known fallback may remain visible. | `none` for normal Stop. | **Start operator** enabled and must create a fresh session. |
| failed | `no` | `no` | `failed` | Last known or `pending`. | Last known or `pending`. | Include fallback only if relevant to failure. | Error message populated, redacted, and metadata-only. | **Start operator** re-enabled after failure cleanup. |

## Runtime backend guidance

### Windows CUDA prerequisites

- Windows 10/11 with an NVIDIA GPU and current NVIDIA drivers.
- Visual Studio C++ build tools, CMake tools for Windows, C++ core features, and
  a Windows SDK.
- CUDA Toolkit installed and visible to the build environment.
- Rebuild the active desktop sidecar interpreter with CUDA support:

```powershell
$env:CMAKE_ARGS = "-DGGML_CUDA=on"
$env:FORCE_CMAKE = "1"
python -m pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

### macOS Metal prerequisites

- macOS host with Apple Silicon or a supported Metal-capable Mac.
- Xcode Command Line Tools and CMake available.
- Rebuild the active desktop sidecar interpreter with Metal support:

```sh
xcode-select --install  # if command line tools are not already installed
brew install cmake
CMAKE_ARGS="-DGGML_METAL=on" FORCE_CMAKE=1 python -m pip install llama-cpp-python --force-reinstall --upgrade --no-cache-dir --verbose
```

### Interpreting backend fields

- `backend_available` is what the platform/runtime probe believes is available
  before or during model initialization (`cuda`, `metal`, or `cpu`).
- `backend_selected` is what desktop chose after applying the requested mode
  (`auto`, `gpu`, `hybrid`, or `cpu`) and any platform/runtime constraints.
- `backend_used` is what the model runtime actually used after initialization;
  this is the authoritative field for release hardware claims.
- `fallback_reason` explains why the selected or used backend is less capable
  than requested. Empty/`none` means no fallback was needed; a populated value is
  expected for CPU fallback tests and a release blocker for GPU-required claims
  unless the reason is intentionally documented.

## Two-node round-robin sign-off

Before publishing production claims that two desktop operators can participate
in round-robin work distribution:

1. Register one Windows CUDA-capable operator and one macOS Metal-capable
   operator, or explicitly document any CPU fallback node used for the test.
2. Confirm both nodes appear in `/relay/diagnostics` with safe routing metadata.
3. Send repeated API v1 E2EE chat requests against the relay.
4. Confirm diagnostics/metrics show both registered nodes polling and receiving
   work over the sample window.
5. Confirm relay logs and diagnostics never contain plaintext prompts or model
   outputs.
