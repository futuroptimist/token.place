# Desktop operator parity contract

This contract defines the platform-neutral behavior that the desktop compute-node operator must preserve across Windows and macOS. The machine-readable test matrix lives in `tests/fixtures/desktop_operator_parity_matrix.json` and should be updated with this document whenever the contract changes.

## Scope

The contract covers the Tauri desktop operator path that starts the Python bridge, prepares the local model runtime, registers with an API v1 relay, processes relay work, and reports lifecycle diagnostics to the UI. API v1 remains non-streaming, and relay-owned state/logs/diagnostics must remain relay-blind: ciphertext payloads plus safe routing metadata only.

## Shared lifecycle requirements

1. **Startup and warm-load**: when warm-load is enabled, the bridge starts model/runtime initialization before relay registration and keeps `registered=false` while the relay runtime state is `starting`, `warming`, or `failed`.
2. **Runtime detection**: runtime detection must report the interpreter, import prefix, `llama_cpp` module path, detected backend, GPU offload support, detected device, and an actionable error when detection fails.
3. **GPU backend selection**: `auto`, `gpu`, and `hybrid` select CUDA on CUDA-capable Windows and Metal on Metal-capable macOS. Explicit `cpu` selects CPU everywhere. CPU fallback is allowed only with a clear `fallback_reason`, and explicit GPU modes must fail closed when platform policy requires GPU support.
4. **Relay registration eligibility**: UI/API status may show `Registered: yes` only after the runtime that will answer relay work is ready and the relay registration is fresh for the active relay URL.
5. **UI field states**: while runtime readiness is pending, `effective_mode`, `backend_available`, `backend_selected`, and `backend_used` are `pending`. Once ready, these fields reflect the runtime that will process relay work, not merely the platform probe.
6. **Stop operator**: Stop emits a stopped status with `running=false`, `registered=false`, and `relay_runtime_state=stopped`, and it releases relay/runtime lifecycle resources.
7. **Start after Stop**: a subsequent Start uses a fresh operator session, drains stale cancel messages, starts a fresh relay session, and can register again after warm-load succeeds.
8. **Packaged resource resolution**: packaged Windows and macOS builds must resolve the same bridge/runtime modules and packaged requirements/resources before falling back to development paths.
9. **Dependency isolation**: desktop-only Python dependencies install into an isolated desktop target such as `.token_place_desktop_site` or a user fallback, never by relying on the repository-local `llama_cpp.py` shim.
10. **Error/fallback behavior**: platform-specific runtime failures must leave `Registered: no` until recovered and must put an actionable, operator-safe message in `last_error` without logging plaintext model payload content.

## Platform matrix

| Matrix entry | Required behavior |
| --- | --- |
| Windows CUDA-capable | CUDA-capable `llama-cpp-python` is selected for `auto`/`gpu`/`hybrid`; failed GPU provisioning in GPU modes fails closed with CUDA recovery guidance. |
| macOS Metal-capable | Metal-capable `llama-cpp-python` is selected for `auto`/`gpu`/`hybrid`; packaged `.app` must use the same bridge/runtime lifecycle as Windows. |
| CPU fallback | CPU mode and unsupported GPU platforms may run through the same warm-load/register/stop lifecycle, but status must disclose CPU fallback and never claim a GPU backend answered relay work. |
| Missing runtime/dependency | The operator fails before relay registration with interpreter/import-root/module diagnostics and an actionable `last_error`. |

## Known gaps captured by tests

- **Prompt 2 gap**: macOS runtime bootstrap is currently probe-only when Metal is missing; tests mark the future Metal bootstrap behavior as an expected failure until Prompt 2 implements it.
- **Prompt 4 gap**: macOS does not yet have the same real packaged operator lifecycle E2E coverage as Windows; tests mark the required E2E parity check as an expected failure until Prompt 4 adds it.
