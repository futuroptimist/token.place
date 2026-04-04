# token.place Tauri desktop client design

## 1) Summary

## Status note (2026-04-04)

Phase 1 MVP is now scaffolded in `desktop-tauri/` with platform preference detection (`Metal / Apple Silicon` on macOS arm64, `CUDA / NVIDIA` on Windows x64, CPU fallback otherwise), a replaceable NDJSON sidecar seam for streaming and cancellation, and optional encrypted relay forwarding via `/next_server` + `/faucet`.

token.place should adopt a Tauri-based desktop client as the forward-looking desktop runtime for local-first LLM usage: run inference on-device via a llama.cpp-backed sidecar, encrypt outputs locally with token.place-compatible cryptography, and forward encrypted payloads into the existing `relay.py` flow without redesigning the network contract. This direction gives token.place a native desktop shell with tighter permission boundaries, a smaller install footprint than Electron-first packaging, and better alignment with a "UI shell + native inference runtime + encrypted forwarding" architecture.

## 2) Problem statement

The browser experience is useful for portability, but it is not sufficient as the primary environment for local-model desktop workflows that need robust process lifecycle management, local model discovery, platform-native key storage, and predictable GPU/CPU inference orchestration.

A dedicated desktop client is needed because token.place is increasingly local-runtime centric: users want model execution near their data, explicit control of hardware acceleration, and encrypted forwarding back to relays without exposing plaintext to network services.

The current Electron direction in `desktop/` is not the right long-term default for this narrow use case. The existing implementation is an earlier prototype oriented around tray/idle scheduling and spawning `server.py`, rather than a clean local-inference client architecture with explicit sidecar contracts, structured streaming events, and first-class encrypted forwarding to `relay.py`.

## 3) Goals

- Provide a local-first desktop shell for token.place local LLM workloads.
- Run local GPU/CPU inference through a native llama.cpp-backed runtime.
- Encrypt outputs locally before network transit and forward ciphertext through existing `relay.py` routes.
- Keep install size and runtime memory overhead small.
- Enforce explicit capability boundaries (filesystem, process, and network).
- Support macOS, Linux, and Windows with a clear supported/experimental policy.
- Support streaming tokens, cancellation, diagnostics, model selection, and progress indicators.
- Preserve compatibility with token.place crypto/client/server abstractions where practical.

## 4) Non-goals

- Replacing the browser client.
- Rewriting `relay.py` or `server.py` in this design phase.
- Inventing a brand-new wire protocol where existing token.place API contracts are sufficient.
- Building a full operator/marketplace desktop surface in v1.
- Solving every packaging/signing/backend-GPU edge case in phase one.

## 5) Why Tauri instead of Electron

Balanced comparison for token.place:

- **App size, memory, startup**: Tauri typically ships smaller artifacts and lower baseline memory for this shape of app, because it reuses the OS webview and keeps core orchestration in Rust.
- **Security posture**: Tauri's capability model (allowlisted APIs, explicit command exposure) maps well to token.place's principle of minimizing ambient authority.
- **Sidecar support**: Both frameworks can launch helper processes; Tauri's command + sidecar pattern works well for a strict boundary around inference.
- **Electron strengths**: Electron has deep Node/ecosystem maturity, broader desktop package examples, and faster onboarding for web-heavy teams that depend on Node APIs.
- **Why token.place favors Tauri**: token.place needs a secure shell around native inference and local encryption rather than a Node-centric desktop app. A tighter native boundary and leaner runtime are directly beneficial.
- **When Electron is better**: Electron remains a better fit when a product needs heavy Node integration, broad plugin ecosystems built around npm desktop tooling, or rapid reuse of existing Electron infrastructure.

Recommendation: choose Tauri as the default for token.place desktop development, while retaining legacy Electron code only as deprecated historical context until retirement.

## 6) Proposed architecture

Recommended boundaries:

- **Frontend UI (Tauri + web UI)**
  - chat/task UI, model picker, job history controls, diagnostics panes.
- **Rust command layer**
  - command handlers for model lifecycle, inference control, encryption requests, relay forwarding.
- **Inference sidecar (llama.cpp runtime)**
  - native process boundary for model execution and GPU backend interaction.
- **Local storage**
  - model metadata, runtime config, and optional encrypted local history.
- **Secure key/settings storage**
  - OS keychain/credential vault for private keys and sensitive relay credentials.
- **Streaming IPC channel**
  - structured events from sidecar to Rust layer to UI (`started`, `token`, `progress`, `stderr`, `done`, `error`).
- **Local encryption stage**
  - encrypt response payload prior to relay upload using token.place-compatible envelope format.
- **Relay forwarding**
  - send encrypted payload to existing `relay.py` endpoints with existing auth/registration semantics.

ASCII overview:

```text
+--------------------------- token.place desktop (Tauri) ---------------------------+
|                                                                                  |
|  UI (Webview) <----invoke/events----> Rust command layer                         |
|      |                                        |                                   |
|      |                          spawn/manage  v                                   |
|      |                              +------------------------+                    |
|      +----streamed tokens/events----| llama.cpp sidecar     |                    |
|                                      | (llama-server/wrapper)|                    |
|                                      +-----------+------------+                    |
|                                                  |                                 |
|                                      model files | logs/metrics                    |
|                                                  v                                 |
|                                    local model/config storage                      |
|                                                                                  |
|  plaintext (local only) -> encrypt locally -> ciphertext envelope -> relay.py     |
+----------------------------------------------------------------------------------+
                                              |
                                              v
                                       relay.py -> server.py
```

Relationship to existing abstractions:

- Reuse token.place encryption envelope formats and API payload expectations where available.
- Keep relay communication aligned with existing endpoints and key exchange assumptions.
- Add shared schema docs if desktop-specific event payloads diverge.

## 7) Inference runtime strategy

Options considered:

1. **`llama.cpp` sidecar binary (baseline sidecar pattern)**
   - Pros: clear process boundary, crash isolation, easier replacement/upgrades, straightforward stderr/stdout capture.
   - Cons: extra packaging complexity and protocol surface between app and sidecar.

2. **`llama-server` sidecar**
   - Pros: stable HTTP interface, rapid bootstrap.
   - Cons: less tailored control over structured events and cancellation semantics unless wrapped.

3. **Custom wrapper process around llama.cpp**
   - Pros: precise event contract and richer telemetry/control.
   - Cons: additional maintenance burden.

4. **Direct library integration in app runtime**
   - Pros: fewer moving parts at runtime.
   - Cons: tighter coupling, harder crash isolation, more complex cross-platform build/signing.

Recommendation:

- Start with a sidecar boundary and prefer either `llama-server` plus a thin adapter or a minimal custom wrapper that emits structured NDJSON/JSON events.
- GPU acceleration is provided by llama.cpp backend builds (Metal/CUDA/Vulkan/ROCm), not by Tauri itself.
- Require explicit handling for:
  - token streaming events,
  - cancellation via signal/control message,
  - stderr/stdout normalization,
  - deterministic error codes for missing/incompatible models.
- Fallback behavior:
  - if GPU backend unavailable, automatically downgrade to CPU with visible warning;
  - if model missing/incompatible, surface remediation actions (select/import/download another model).

## 8) Security and privacy model

Threat model highlights:

- Local malware/admin compromise can access local plaintext while jobs run.
- Relay/network observers should only see encrypted envelopes and metadata needed for routing.
- Remote service operators must not receive raw prompts/responses from desktop before encryption.

Plaintext lifecycle:

- Plaintext exists in-memory locally during prompt entry, inference, and pre-encryption output assembly.
- Plaintext should not be persisted by default.

Encryption and relay visibility:

- Encrypt payloads locally before any relay upload.
- `relay.py` should see only ciphertext envelope + required routing metadata, not prompt/output plaintext.

Key handling:

- Store long-lived private keys in OS secure storage (Keychain, Credential Manager, libsecret-backed provider).
- Avoid writing raw keys to normal config files.

Permission posture:

- Do not request broad shell/filesystem capabilities by default.
- Limit filesystem scope to model directories and app data paths selected by user.

Logging policy:

- Default logs must exclude plaintext prompts, outputs, and raw ciphertext bodies.
- Diagnostics should use redacted metadata (timings, model id, backend, token counts, error codes).

History policy:

- Default: history disabled or ephemeral.
- If enabled, make history opt-in and encrypted at rest.

## 9) Platform support strategy

Recommended rollout order:

1. **macOS (Apple Silicon first)**
   - strong local-LLM user base and predictable Metal acceleration path.
2. **Windows (x64, optional NVIDIA-first acceleration path)**
   - significant user demand, but packaging/signing and CUDA distribution are more complex.
3. **Linux (x64 first)**
   - broad ecosystem, but distro/GPU-stack fragmentation requires explicit experimental labeling early.

Initial support tiers:

- **Supported**: tested in CI/nightly smoke plus manual release validation.
- **Experimental**: best effort, community feedback loop, known limitations documented.

Backend notes:

- macOS: Metal-enabled llama.cpp builds should be first-class.
- Windows: CUDA packaging and driver/runtime compatibility need explicit matrix docs.
- Linux: Vulkan/ROCm/NVIDIA paths likely require distro-specific guidance.

## 10) Packaging and distribution

- Use Tauri bundling for per-platform installers and app metadata.
- Package inference sidecar as versioned platform-specific artifact, checksum-verified at build/release time.
- Plan for platform trust requirements:
  - macOS code signing + notarization (Gatekeeper),
  - Windows code signing + SmartScreen reputation considerations,
  - Linux package signing per target format where feasible.
- Model strategy (v1): prioritize bring-your-own-model path with optional guided download later.
- Updates: support app auto-update channel independent from model binaries when possible.
- Mitigate AV false positives by deterministic builds, signatures, and transparent release notes for bundled binaries.

## 11) UX flows

1. **First launch**
   - privacy summary, permission summary, choose model folder, optional telemetry toggle.
2. **Model selection/import**
   - detect existing GGUF files, validate compatibility, display backend capabilities.
3. **Compute mode selection**
   - Auto / CPU-only / GPU-preferred with fallback explanation.
4. **Start inference task**
   - submit prompt, launch sidecar job, stream partial output.
5. **Streaming + cancellation**
   - token-by-token updates, stop button with deterministic cancellation state.
6. **Encrypt + forward result**
   - post-processing step encrypts output and sends through relay path.
7. **Relay unreachable**
   - clear error, retry/backoff choice, optional local queue decision (future).
8. **Sidecar crash**
   - recoverable error UI with restart option and diagnostic bundle.
9. **Missing/incompatible model**
   - guided remediation (pick another model, show required backend).
10. **Logs/diagnostics**
   - dedicated view with redacted operational events only.

## 12) Integration boundaries

Required contracts:

- **UI ↔ native/sidecar**
  - typed request/response + event schema for start/stream/cancel/status/error.
- **Desktop app ↔ `relay.py`**
  - reuse existing token.place encrypted payload contracts and auth headers where possible.
- **Reuse opportunities**
  - existing encryption envelope schema and relay endpoint expectations.

Potential enhancements:

- Introduce shared JSON schema docs for desktop sidecar events and encrypted forwarding envelopes.
- Decide whether desktop talks directly to `relay.py` or through an optional localhost helper abstraction:
  - v1 recommendation: direct to relay contract from desktop Rust layer;
  - future option: localhost helper only if protocol/version mediation becomes necessary.

## 13) Migration / retirement plan from Electron

Current repo state:

- `desktop/package.json` and `desktop/electron-builder.json` are Electron-based.
- `desktop/src/main/Main.ts` and `desktop/src/main/IdleScheduler.ts` reflect a legacy desktop prototype focused on tray/idle-triggered `server.py` lifecycle.

Why this path is being turned down:

- It does not represent the intended local-LLM desktop architecture (sidecar inference + local encryption + relay forwarding).
- It risks directing contributors toward maintaining an implementation shape that is no longer strategic.

Status recommendation:

- Mark current `desktop/` implementation as **deprecated legacy prototype**.
- Freeze for historical reference and short-term compatibility only.
- Plan retirement/removal in a later implementation PR once Tauri replacement reaches parity for required workflows.

Immediate cleanup actions in this PR:

- Add this design doc as the canonical forward-looking desktop architecture.
- Update docs to state that Electron is legacy/deprecated and Tauri is the recommended direction.
- Add a prominent deprecation note inside `desktop/`.

Follow-up tasks (later PRs):

- Scaffold `desktop-tauri/` or equivalent Tauri workspace.
- Define sidecar event schema + relay forwarding contract tests.
- Implement phased migration and eventually archive/remove legacy Electron code.

Risk if stale Electron references remain:

- New contributors may invest effort in the deprecated path, causing architectural drift and duplicated maintenance.

## 14) Risks and open questions

- Should v1 sidecar be `llama-server` or a custom wrapper for richer control?
- Where should crypto implementation live long-term (Rust-native, reused JS/Python logic, or mixed)?
- How should large encrypted payloads be chunked/retried safely?
- Should offline queueing be in scope early or deferred?
- What is the right model-management UX for large files and backend compatibility checks?
- Should inference remain permanently as a subprocess boundary?
- What telemetry/observability data can be collected while preserving privacy guarantees?

## 15) Phased implementation plan

- **Phase 0 (this stage)**: design finalized, Electron direction explicitly deprecated.
- **Phase 1**: Tauri shell scaffold, settings UI, placeholder sidecar, smoke tests.
- **Phase 2**: local llama.cpp inference with streaming and cancellation.
- **Phase 3**: local encryption + relay.py forwarding integration.
- **Phase 4**: packaging/signing hardening across target platforms.
- **Phase 5**: model management polish, resilience features, advanced UX.

## 16) Test strategy

Future test plan for desktop implementation:

- **Unit tests**
  - Rust command handlers, config parsing, sidecar event parser, encryption envelope assembly.
- **UI ↔ sidecar integration tests**
  - start/stream/cancel flows with deterministic mocked sidecar outputs.
- **Crypto compatibility/contract tests**
  - desktop encryption envelopes must round-trip with existing token.place server/relay expectations.
- **Relay compatibility tests**
  - validate request/response semantics against `relay.py` in local integration harness.
- **GPU fallback tests**
  - ensure CPU fallback path is explicit when acceleration backend missing.
- **End-to-end smoke tests**
  - at least one supported platform in CI/nightly with a tiny model fixture.
- **CI alignment**
  - integrate desktop checks into existing `pre-commit run --all-files`/`run_all_tests.sh` conventions once implementation starts.
