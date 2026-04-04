# token.place Tauri desktop client design

## 1) Summary

token.place should introduce a Tauri-based desktop client that runs local LLM inference on-device,
performs local encryption before any network transfer, and forwards encrypted results through the
existing `relay.py` flow. This client addresses workflow gaps for users who need long-running local
inference, richer native controls, and explicit local security boundaries that are difficult to
express in a browser-only runtime. Tauri is the recommended forward path because token.place's
shape is "desktop UI shell + native inference runtime + encrypted forwarding," where small
footprint, strong permission controls, and first-class sidecar process orchestration matter more
than broad Node/Electron ecosystem leverage.

## 2) Problem statement

Browser-based usage remains essential, but it is insufficient as the primary experience for local
model operators who need predictable access to local files, model lifecycle controls, resource
selection, background execution, and crash-isolated native processes. token.place needs a dedicated
desktop client to make local inference ergonomic and secure while preserving compatibility with the
existing encrypted relay architecture. The current Electron direction in `desktop/` is not the
right long-term default because it is centered on an older tray/idle scheduler pattern that starts
`server.py` (`desktop/src/main/Main.ts`, `desktop/src/main/IdleScheduler.ts`) rather than a
purpose-built local inference + encrypted-forwarding client, and it carries a larger runtime
footprint than needed for this narrow use case.

## 3) Goals

- Provide a local-first desktop shell focused on token.place inference workflows.
- Run local GPU/CPU inference through a native `llama.cpp`-backed runtime.
- Encrypt outputs locally before forwarding them through existing `relay.py` contracts.
- Keep installer size and baseline memory usage low for operator laptops/desktops.
- Enforce explicit permission boundaries (filesystem, process, network).
- Deliver a realistic cross-platform path for macOS, Linux, and Windows.
- Support forward-compatible UX primitives: streaming tokens, cancellation, logs, model selection,
  and progress feedback.

## 4) Non-goals

- Replacing the browser client (`static/`) or CLI clients.
- Rewriting `relay.py` or `server.py` as part of this design phase.
- Inventing a new network protocol when existing token.place contracts can be reused.
- Building a full marketplace/operator-management desktop control plane in v1.
- Fully solving every packaging/signing/GPU edge case in the first implementation phase.

## 5) Why Tauri instead of Electron

### Balanced comparison

- **App size / memory / startup**
  - Tauri typically ships smaller binaries because it uses OS webviews instead of bundling Chromium
    per app.
  - Electron remains viable but generally costs more idle memory and larger installers.
- **Security posture**
  - Tauri provides capability-based permission configuration and a narrower default bridge between
    UI and native operations.
  - Electron can be secured, but requires more manual hardening discipline around preload, context
    isolation, and Node access.
- **Sidecar support**
  - Both can run subprocesses. Tauri's Rust command model and sidecar packaging are a strong fit
    for deterministic local runtime boundaries.
- **Ecosystem strengths for Electron**
  - Electron still offers mature Node-first desktop libraries, broad examples, and quick iteration
    for web teams.
- **Why token.place favors Tauri**
  - token.place needs a thin native shell around local inference and encryption, not a heavy
    browser runtime plus broad plugin surface.
  - The project benefits from explicit capability declarations and smaller distribution artifacts.
- **When Electron would be better**
  - If token.place needed deep Node desktop integrations unavailable in Rust/Tauri, or extremely
    rapid reuse of a large Electron-specific extension ecosystem, Electron could still be a better
    choice.

## 6) Proposed architecture

### Component boundaries

- **Tauri frontend UI (TypeScript/React)**
  - Job setup, model selection, streaming output view, diagnostics.
- **Tauri Rust command layer**
  - Permission-gated commands for process control, filesystem access, and secure settings.
- **Inference sidecar**
  - Native `llama.cpp` runtime process (`llama-server` or wrapper) launched and supervised by the
    Rust layer.
- **Local storage**
  - App config, model metadata, and optional encrypted job history in app-specific data dirs.
- **Secure key storage**
  - OS keychain/credential vault for long-lived keys or wrapping keys.
- **IPC/event channel**
  - Structured events from sidecar to UI: started, token chunk, progress, warning, error,
    completed.
- **Local encryption stage**
  - Encrypt payload before any relay POST.
- **Relay forwarding**
  - Reuse `relay.py` endpoints and existing token.place crypto envelope expectations.

### ASCII diagram

```text
+------------------------------ token.place desktop (Tauri) ------------------------------+
|                                                                                          |
|  React UI  <----invoke/events---->  Rust command layer  <----stdio/socket---->  Sidecar |
|    |                                      |                                      llama.cpp|
|    |                                      |                                              |
|    +---- local plaintext view -----------+                                              |
|                                           \                                             |
|                                            +--> local encryption --> ciphertext payload  |
+------------------------------------------------------------------------------------------+
                                             |
                                             v
                                      relay.py (opaque)
                                             |
                                             v
                                    token.place server flow
```

### Relationship to existing abstractions

- Keep token.place crypto contracts compatible with the existing client/server expectations.
- Reuse existing relay/server message envelopes where possible to avoid protocol drift.
- Document contract changes in shared schema docs before implementation.

## 7) Inference runtime strategy

### Options

1. **`llama-server` sidecar (recommended initial path)**
   - Pros: battle-tested HTTP/streaming behavior, easy process isolation, backend flags for
     Metal/CUDA/Vulkan.
   - Cons: extra translation layer between UI and server semantics.
2. **Custom wrapper around `llama.cpp` binaries**
   - Pros: tighter contract control and smaller exposed API.
   - Cons: more maintenance burden, more runtime edge cases to own.
3. **Direct library integration in Rust**
   - Pros: fewer process hops, potentially tighter memory control.
   - Cons: higher build complexity across platforms/toolchains; harder short-term delivery.

### Recommendation

Start with a sidecar boundary (`llama-server` first, custom wrapper later if needed). GPU access is
provided by `llama.cpp` backend builds (Metal, CUDA, Vulkan/ROCm), not by Tauri itself. The Tauri
layer should focus on orchestration: launch config, health checks, streaming and cancellation,
stdout/stderr capture, and structured event mapping.

### Runtime behavior expectations

- Streaming tokens: map sidecar stream chunks to typed UI events.
- Cancellation: terminate request (or process as fallback) with explicit user feedback.
- stderr/stdout: capture and classify as diagnostics; redact sensitive payloads.
- No GPU available: auto-fallback to CPU with visible warning and persisted preference override.
- Missing/incompatible model: deterministic error state with guided recovery path (select model,
  reindex, redownload, or change backend).

## 8) Security and privacy model

### Threat model

- Malicious relay or network observers.
- Local malware or over-privileged desktop runtime.
- Accidental plaintext leakage via logs or crash reports.

### Data handling

- Plaintext prompt/output exists only in local memory during active inference and rendering.
- Before leaving device boundaries, payloads are encrypted with token.place-compatible envelopes.
- `relay.py` sees routing metadata and ciphertext only; it cannot inspect prompt/response content.

### Key/storage guidance

- Store long-lived private material in OS-managed keychain/credential stores.
- Avoid broad read/write filesystem permissions; scope access to explicit model + app dirs.
- Disallow arbitrary shell execution from UI-initiated commands.

### Logging policy

- Default: no plaintext prompt/output persisted.
- Diagnostic logs should be metadata-only unless user explicitly opts into verbose local logging.
- Job history should default to disabled or encrypted-at-rest opt-in with clear retention controls.

## 9) Platform support strategy

- **Recommended rollout**: macOS (Apple Silicon) first, then Windows, then Linux.
- **macOS first rationale**: stable local inference demand, good Metal support in `llama.cpp`.
- **Windows**: expect higher CUDA packaging/signing/support overhead.
- **Linux**: support likely split across distro/toolchain/GPU backend combinations.

Initial support definitions:

- **Supported**: tested in CI/release checklist, documented install path, known-good backend matrix.
- **Experimental**: best-effort builds with limited QA and explicit caveats.

## 10) Packaging and distribution

- Use standard Tauri bundling for platform installers.
- Package inference sidecar with explicit version pinning and checksum verification.
- Plan for platform trust requirements:
  - macOS: code signing + notarization (Gatekeeper).
  - Windows: signing + SmartScreen reputation ramp.
  - Linux: signed artifacts where practical and checksum distribution.
- Model strategy in v1: prioritize bring-your-own-model plus optional guided download flow.
- Updates: staged auto-update channel after initial stable manual release path.
- Expect AV false positives on bundled inference binaries; document verification and signatures.

## 11) UX flows

1. **First launch**: onboarding, permission prompts, storage location selection, privacy defaults.
2. **Model setup**: detect existing GGUF files or import path; validate compatibility.
3. **Runtime mode**: choose Auto/CPU/GPU with detected backend hints.
4. **Run task**: submit prompt, observe startup/progress states.
5. **Streaming**: token-by-token UI updates with latency and cancel controls.
6. **Encrypt + forward**: completion payload encrypted locally, forwarded via relay flow.
7. **Relay unreachable**: queue retry option or export encrypted payload; clear status messaging.
8. **Sidecar crash**: capture crash diagnostics, offer restart, preserve unsent encrypted output.
9. **Model/backend mismatch**: actionable error and guided remediation.
10. **Diagnostics view**: local-only logs with redaction and user-controlled export.

## 12) Integration boundaries

- **UI ↔ native contract**: typed commands/events (`startInference`, `cancelInference`,
  `streamChunk`, `runtimeError`, `encryptAndForward`).
- **Desktop ↔ relay.py contract**: keep existing HTTP endpoints and encryption envelope semantics.
- **Reuse opportunities**: existing token.place crypto schema and API compatibility assumptions.
- **Future standardization**: introduce shared schema docs for stream events and encrypted payload
  envelopes to reduce drift across browser/CLI/desktop.
- **Transport choice**: desktop can call `relay.py` directly in v1; optional localhost helper is a
  future abstraction if retries/queueing become complex.

## 13) Migration / retirement plan from Electron

### Current repo state

- `desktop/package.json` defines Electron + `electron-builder` scripts.
- `desktop/src/main/Main.ts` and `desktop/src/main/IdleScheduler.ts` implement a legacy tray/idle
  orchestration path that launches `server.py` and focuses on background scheduling.

### Decision

Turn down the Electron direction as default and mark it **deprecated/frozen**. Keep source for
historical reference until Tauri implementation reaches functional parity for core local inference
flows.

### Immediate cleanup actions in this PR

- Add this Tauri design doc under `docs/design/`.
- Update repo-level docs so contributors see Tauri as the forward path.
- Add explicit deprecation notice in `desktop/README.md`.

### Follow-up tasks (later PRs)

- Scaffold `desktop-tauri/` (or equivalent) with minimal shell and CI smoke checks.
- Add shared desktop contract schemas and crypto compatibility tests.
- Freeze or remove Electron build workflows/scripts once Tauri replacement is stable.

### Risk if stale Electron references remain

Contributors may continue investing in an architecture the project no longer intends to ship,
causing split effort, inconsistent security assumptions, and migration drag.

## 14) Risks and open questions

- `llama-server` sidecar vs custom wrapper long-term ownership.
- Crypto placement: Rust-native implementation, reused JS/Python logic, or hybrid bridge.
- Large encrypted payload handling and chunking semantics.
- Offline queueing vs immediate forwarding-only behavior.
- Model lifecycle UX (downloads, integrity checks, upgrades, cleanup).
- Whether subprocess isolation remains permanent or evolves into deeper native integration.
- Telemetry/observability strategy aligned with privacy guarantees.

## 15) Phased implementation plan

- **Phase 0**: design + deprecate Electron direction (this docs PR).
- **Phase 1**: Tauri shell, placeholder sidecar wiring, settings storage, smoke tests.
- **Phase 2**: local `llama.cpp` inference integration with streaming and cancellation UX.
- **Phase 3**: local encryption + relay.py forwarding compatibility path.
- **Phase 4**: packaging, signing, notarization, platform hardening.
- **Phase 5**: model management, resilience features, richer diagnostics and advanced UX.

## 16) Test strategy

Future implementation PRs should include:

- Unit tests for Rust command handlers, config parsing, and event mapping.
- UI↔sidecar integration tests for streaming, cancellation, and failure modes.
- Crypto compatibility/contract tests against existing token.place envelopes.
- Relay compatibility tests validating encrypted forwarding behavior with `relay.py`.
- GPU fallback tests (GPU unavailable, low VRAM, backend mismatch).
- End-to-end desktop smoke tests on at least one supported platform in CI.
- CI alignment with current repository expectations by extending existing pre-commit/`run_all_tests`
  flows rather than creating an isolated test island.
