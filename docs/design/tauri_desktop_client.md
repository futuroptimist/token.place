# token.place Tauri desktop client design

## 1) Summary

token.place should adopt a **Tauri-based desktop client** as the forward-looking
local-model experience: a lightweight desktop shell that runs local llama.cpp-backed
inference, encrypts outputs locally, and forwards encrypted payloads through the
existing `relay.py` flow. This direction fits token.place’s privacy-first
architecture and local-compute mission better than the current Electron prototype,
because Tauri offers tighter capability boundaries, smaller runtime footprint, and a
clearer native-process integration model for inference sidecars.

## 2) Problem statement

A browser-only experience is not sufficient for the local-model workflow token.place
is targeting:

- Local inference needs predictable access to model files, accelerator selection,
  long-running processes, and robust cancellation/recovery semantics.
- Browser sandboxes and inconsistent local hardware access make durable offline or
  partially-offline inference UX difficult.
- A dedicated desktop app can provide better process lifecycle control, logs,
  permissions, and packaging for local runtimes.

Why the existing Electron direction should not remain the default:

- The current desktop implementation is an older tray/idle scheduler path that
  spawns `server.py` during idle windows, which does not match the desired
  local-first interactive inference client direction.
- Electron is viable in general, but token.place’s target shape (UI shell + native
  inference runtime + local crypto + encrypted forwarding) benefits from smaller
  runtime and tighter native permission boundaries than a bundled Chromium+Node
  baseline.

## 3) Goals

- Provide a local-first desktop shell for local LLM workloads.
- Run local GPU/CPU inference via a native llama.cpp-backed runtime.
- Encrypt inference outputs locally before any network forwarding.
- Forward encrypted payloads through existing token.place relay contracts
  (`relay.py`) with minimal protocol churn.
- Keep installer/runtime footprint small.
- Enforce explicit permission boundaries (filesystem/process/network).
- Support macOS, Linux, and Windows with phased maturity levels.
- Support future UX features: token streaming, cancellation, structured logs, model
  selection, and progress reporting.

## 4) Non-goals

- Replacing or removing the existing browser client.
- Rewriting `relay.py` or `server.py` as part of this design PR.
- Designing a net-new wire protocol when existing token.place contracts can be
  reused.
- Shipping a full marketplace/operator management desktop suite in v1.
- Fully solving all signing, packaging, GPU backend, and driver edge cases in phase
  one.

## 5) Why Tauri instead of Electron

Balanced comparison for token.place:

### Tauri advantages for this use case

- **Smaller binary/runtime footprint**: Tauri uses system webview + Rust backend,
  usually yielding smaller installers and lower baseline memory than shipping full
  Chromium.
- **Capability-based security posture**: Tauri’s allowlist/capability model helps
  avoid broad default access and makes privileged operations explicit.
- **Native sidecar ergonomics**: Tauri supports launching and supervising external
  binaries, which maps well to llama.cpp sidecars.
- **Startup and idle resource profile**: for a utility-style local inference shell,
  minimizing always-on desktop overhead is valuable.

### Electron strengths (acknowledged)

- Rich Node.js ecosystem and mature desktop packaging patterns.
- Broad community examples for updater, crash reporting, and native module
  integration.
- Easier reuse when a project is already deeply invested in Node main-process
  infrastructure.

### Recommendation

For token.place specifically—desktop UI shell + native inference runtime + local
encryption + encrypted forwarding—Tauri is the better default due to tighter
permission boundaries and lower runtime overhead. Electron would still be reasonable
if the project needed heavy Node-native main-process plugins or Chromium-specific
runtime features that are hard to reproduce in Tauri.

## 6) Proposed architecture

### Components

- **Tauri frontend UI (WebView)**
  - Job authoring, model/runtime selection, streaming display, diagnostics.
- **Tauri Rust command layer**
  - Invokes sidecar, manages lifecycle, validates settings, orchestrates encryption
    and relay forwarding.
- **Inference sidecar process**
  - llama.cpp-based runtime (`llama-server` or custom wrapper), isolated as
    subprocess.
- **Local storage**
  - Model paths, runtime config, optional encrypted local history.
- **Secure key/settings storage**
  - OS keychain/credential store for private key material where possible.
- **Relay forwarder adapter**
  - Reuses token.place request envelopes and posts encrypted payloads to `relay.py`.

### Recommended boundary model

- UI never directly shells out to inference binaries.
- Rust command layer is the only trusted mediator for process launch and network
  forwarding.
- Sidecar emits structured events consumed by Rust, then bridged to UI.
- Encryption occurs before relay egress; relay only sees opaque ciphertext envelope
  and metadata required for routing.

### ASCII architecture diagram

```text
+--------------------+       invoke/events       +---------------------------+
| Tauri UI (WebView) | <-----------------------> | Tauri Rust command layer  |
| - prompts/settings |                           | - policy + orchestration  |
| - stream rendering |                           | - encryption + forwarding |
+---------+----------+                           +------------+--------------+
          |                                                       |
          | local IPC                                             | spawn/supervise
          v                                                       v
+--------------------------+                           +------------------------+
| Local secure storage     |                           | llama.cpp sidecar      |
| - settings               |                           | (llama-server/wrapper) |
| - key references         |                           | - CPU/GPU inference    |
+--------------------------+                           | - token stream events  |
                                                       +-----------+------------+
                                                                   |
                                                                   | encrypted payload only
                                                                   v
                                                         +----------------------+
                                                         | relay.py             |
                                                         | (opaque forwarding)  |
                                                         +----------+-----------+
                                                                    |
                                                                    v
                                                         +----------------------+
                                                         | token.place server.py |
                                                         +----------------------+
```

## 7) Inference runtime strategy

### Options

1. **`llama-server` sidecar binary**
   - Pros: mature HTTP interface, built-in streaming patterns, easier process
     isolation.
   - Cons: additional local HTTP layer and parsing overhead.
2. **Custom wrapper sidecar around llama.cpp**
   - Pros: tighter protocol control, structured events from first principles.
   - Cons: more code to build and maintain.
3. **Direct library integration in Rust app process**
   - Pros: fewer process boundaries.
   - Cons: larger blast radius on crashes, harder upgrades, platform build
     complexity.

### Recommendation

Start with a **sidecar boundary**. Prefer `llama-server` first for fast delivery and
known behavior, with an explicit path to replace it with a custom wrapper if
contract control or performance requires it.

Key behavior requirements:

- Stream tokens/events incrementally to UI.
- Support cancellation by signaling sidecar request/session termination.
- Parse and route stdout/stderr as structured diagnostics (not raw unbounded logs).
- Detect missing/incompatible model files before inference start.
- Fall back to CPU mode when GPU backend is unavailable or fails initialization.

Clarification: GPU acceleration is provided by llama.cpp backends
(Metal/CUDA/Vulkan/etc.), not by Tauri itself.

## 8) Security and privacy model

### Threat model

- Protect prompt/response plaintext from relay and intermediate transport layers.
- Minimize accidental local leakage through logs, crash dumps, and debug tooling.
- Limit damage from compromised UI code by restricting privileged operations to the
  Rust layer.

### Plaintext lifecycle

Plaintext may exist locally in:

- Prompt input in UI memory.
- Sidecar input/output buffers during active inference.
- Optional transient diagnostics.

Plaintext must not be transmitted off-device before encryption.

### Encryption and relay visibility

- Encrypt result payloads locally before network egress.
- Relay sees only ciphertext envelope + required routing metadata.
- Relay cannot read prompt/response content.

### Key management

- Private key material should live in OS-provided secure storage where possible.
- Avoid writing raw private keys into plaintext config files.
- Rotate keys on demand; consider periodic rotation policy in later phases.

### Permission minimization

- Restrict filesystem access to configured model/cache directories.
- Avoid blanket shell permissions; only allow explicit sidecar binary execution.
- Restrict network egress to configured relay endpoints unless developer mode is
  enabled.

### Logging policy

- Default logs: operational metadata only (durations, model id/path hash, error
  codes).
- No prompt/response plaintext in normal logs.
- Verbose diagnostics must be explicit opt-in with redaction safeguards.
- Local job history should be disabled by default or stored encrypted when enabled.

## 9) Platform support strategy

Recommended rollout:

1. **macOS (Apple Silicon) first**
   - Strong local-LLM adoption and reliable Metal backend path.
2. **Windows next**
   - High user demand, but CUDA packaging/signing complexity is higher.
3. **Linux after core stabilization**
   - Broader distro/backend fragmentation (Vulkan/ROCm variants).

Support tiers initially:

- **Supported**: validated in CI + manual smoke on target hardware, documented known
  limits.
- **Experimental**: basic launch path works, limited guarantees, may require manual
  runtime setup.

## 10) Packaging and distribution

- Use Tauri-native bundling for platform packages.
- Package sidecar as a versioned artifact matched to app version and target
  platform.
- Handle platform trust requirements:
  - macOS: signing + notarization for Gatekeeper.
  - Windows: Authenticode signing to reduce SmartScreen friction.
  - Linux: signed archives/packages where ecosystem supports it.
- Model strategy (v1): bring-your-own-model path first, optional curated downloader
  later.
- Updates: phased auto-update only after signing pipeline is stable.
- Mitigate AV false positives by reproducible builds, signing, and transparent hash
  publication.

## 11) UX flows

### First launch

- Explain local inference model and privacy boundaries.
- Collect relay endpoint + basic telemetry/logging consent.
- Check sidecar availability and runtime compatibility.

### Model setup

- Let user choose/import model file path.
- Validate format/backend compatibility before first run.
- Offer CPU/GPU/Auto execution mode.

### Inference flow

- User starts local task.
- UI displays streaming tokens and progress.
- User can cancel at any time.

### Forwarding flow

- On completion (or checkpoints), app encrypts result locally.
- App forwards encrypted payload to configured `relay.py` endpoint.
- UI shows delivery status and retry controls.

### Error flows

- Relay unreachable: queue transient retry or present explicit retry action.
- Sidecar crash: preserve non-sensitive diagnostics and offer restart.
- Missing/incompatible model: actionable remediation steps.
- Diagnostics view: operational logs only, with explicit redaction notice.

## 12) Integration boundaries

Required contracts:

- **UI ↔ native layer**: typed commands/events for run/cancel/status/logs.
- **Native layer ↔ sidecar**: stable request/stream/cancel schema; versioned.
- **Desktop ↔ relay.py**: reuse existing token.place encrypted payload contracts
  wherever possible.

Reuse priorities:

- Reuse existing token.place crypto envelope format and relay endpoints.
- Reuse existing API model naming where practical.

Potential future hardening:

- Shared schema package (JSON schema/OpenAPI fragments) for desktop, Python
  services, and tests.
- Optional localhost helper abstraction for advanced queueing/retries if direct
  relay coupling becomes brittle.

## 13) Migration / retirement plan from Electron

### Current state in this repository

`desktop/` currently contains a legacy Electron prototype (TypeScript
main/renderer, tray helpers, idle scheduling, electron-builder scripts).

### Why this direction is being turned down

- It is optimized around idle scheduler/tray semantics and launching `server.py`,
  not around dedicated local-inference job orchestration.
- It risks steering contributors toward a runtime architecture that is no longer
  preferred.

### Recommended retirement posture

- Mark Electron desktop as **deprecated/frozen** immediately.
- Keep source for historical reference in the short term.
- Plan explicit archival/removal once Tauri replacement reaches minimum parity.

### Immediate cleanup actions in this PR

- Add this design doc (`docs/design/tauri_desktop_client.md`).
- Update repo docs to identify `desktop/` as a deprecated Electron prototype.
- Add a clear deprecation notice in `desktop/README.md` pointing to this design.

### Follow-up implementation tasks (later PRs)

- Scaffold `desktop-tauri/` (or equivalent) with minimal shell and settings.
- Introduce sidecar management + streaming contract.
- Add local encryption + relay forwarding path with contract tests.
- Sunset Electron packaging scripts and CI jobs once unused.

### Risk if stale Electron docs/scripts remain

Contributors may continue adding Electron-specific code and automation, increasing
migration cost and creating conflicting desktop directions.

## 14) Risks and open questions

- `llama-server` sidecar vs custom wrapper: where should long-term control live?
- Crypto implementation location: Rust-native, reused JS/Python logic, or hybrid
  shared contract?
- Large payload strategy: chunking, compression, or bounded payload handoff to
  relay.
- Offline queueing policy: immediate-forward only vs durable local queue.
- Model management UX: BYO-only vs integrated downloader and checksum verification.
- Subprocess boundary permanence: keep sidecar isolation long-term or fold into
  native runtime later.
- Privacy-aligned observability: which minimal telemetry signals are acceptable by
  default.

## 15) Phased implementation plan

- **Phase 0 (this PR):** design finalization + deprecate Electron direction in
  docs.
- **Phase 1:** Tauri shell + capability config + placeholder sidecar + settings +
  smoke tests.
- **Phase 2:** llama.cpp local inference integration with streaming UI and
  cancellation.
- **Phase 3:** local encryption and relay forwarding compatibility with `relay.py`
  contracts.
- **Phase 4:** signing/notarization, packaging hardening, and platform
  stabilization.
- **Phase 5:** model management UX, resilience (retry/queue), richer diagnostics.

## 16) Test strategy (for implementation phases)

- **Unit tests**
  - Command-layer validation, config parsing, key handling, redaction behavior.
- **UI ↔ sidecar integration tests**
  - Start/stream/cancel/error state transitions.
- **Crypto compatibility tests**
  - Desktop-generated envelopes decrypt correctly with existing token.place
    services.
- **Relay compatibility tests**
  - Verify encrypted payload forwarding against local `relay.py` fixture.
- **GPU fallback tests**
  - Explicitly exercise unavailable/failed GPU backend and CPU fallback path.
- **E2E smoke tests**
  - At least one fully supported platform per release (starting with macOS).
- **CI alignment**
  - Add desktop test target to existing CI stages incrementally, keeping
    `pre-commit` and `run_all_tests.sh` expectations coherent with the
    repository’s current gating model.
