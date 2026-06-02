# Desktop operator parity contract

This contract is the source of truth for cross-platform token.place desktop compute-node behavior on Windows and macOS. The desktop app should use shared Tauri/Rust/Python bridge code plus small platform capability adapters; it must not fork operator lifecycle behavior by OS.

The machine-readable matrix in `tests/fixtures/desktop_operator_parity_matrix.json` mirrors this contract for unit tests.

## Platforms and runtime capabilities

| Platform path | Expected capability | Bootstrap expectation |
| --- | --- | --- |
| Windows CUDA-capable | Detect CUDA-capable `llama-cpp-python` and select CUDA for GPU/auto modes. | If GPU runtime is missing and bootstrap is explicitly enabled, repair or install a CUDA-capable runtime; fail closed with actionable diagnostics when GPU mode cannot be provisioned. |
| macOS Metal-capable | Detect Metal-capable `llama-cpp-python` and select Metal for GPU/auto modes. | If GPU runtime is missing and bootstrap is explicitly enabled, repair or install a Metal-capable runtime; fail closed with actionable diagnostics when GPU mode cannot be provisioned. |
| CPU fallback | Honor explicit CPU mode and report CPU backend fields. | Do not attempt GPU bootstrap in CPU mode. |
| Missing runtime/dependency | Fail closed before relay registration when bridge dependencies are unavailable. | Report the missing interpreter/import root/module or install failure without logging plaintext payloads. |

## Shared lifecycle contract

1. **Startup / warm-load**: the bridge emits `started` first, then warms the same Python runtime that will process API v1 relay work before registration.
2. **Runtime detection**: `backend_available`, `backend_selected`, and `backend_used` must describe the active relay-processing runtime, not a stale probe or a UI-only runtime.
3. **GPU backend selection**: Windows CUDA and macOS Metal are equivalent GPU-capable outcomes. CPU is valid for explicit CPU mode and may be used for documented non-GPU fallback cases, but CPU fallback never by itself authorizes relay registration; registration still waits for a warmed relay-processing runtime. Missing `llama_cpp` is not a CPU fallback and must fail closed until a usable runtime is installed.
4. **Relay registration eligibility**: `registered: true` is allowed only when the operator is running, the relay runtime is `ready` or `processing`, warm-load has completed, and API v1 relay registration is fresh for the active relay URL. Missing desktop runtime dependencies, failed runtime setup, and `probe_only`/pending bootstrap states are not registration-eligible; `registered` stays `false` until the relay-processing runtime proves readiness.
5. **UI field states**: while the runtime is `starting` or `warming`, effective/backend fields remain `pending`; once ready, fields reflect the active runtime. `Last error` is empty when healthy and actionable when not.
6. **Stop operator**: Stop cancels polling, unregisters from the relay when possible, stops the relay client, emits `stopped`, and reports `registered: false`.
7. **Start after Stop**: a new start gets a fresh operator session and sequence stream; stale stop/error events from an old session must not overwrite the active session.
8. **Packaged resource resolution**: packaged Windows and macOS apps must resolve the same bridge script, runtime import root, Python launcher, and requirements paths used by development flows.
9. **Dependency isolation**: desktop dependency installs use the desktop-specific target/import root and avoid user-site leakage; repo-local shims must not shadow site-packages runtime modules.
10. **Error/fallback behavior**: relay-owned logs/state remain ciphertext-only plus safe routing metadata. Any runtime, dependency, or registration failure must fail closed with diagnostics that explain platform, action, interpreter/import root when relevant, and a next step.

## Validation checklist

The release and development checklist that keeps this contract evergreen lives in [Desktop parity validation checklist](../desktop_parity_validation.md). Use that shared checklist for Windows CUDA, macOS Metal, CPU fallback, packaged resource resolution, warm-load, API v1 E2EE relay registration/chat, Stop, Start after Stop, and two-node round-robin release evidence.
