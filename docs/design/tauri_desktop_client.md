# token.place Tauri desktop client design

Status: Proposed (Phase 0 design)

## 1) Summary

token.place should add a Tauri-based desktop client focused on a local-first workflow: run
llama.cpp-backed inference on-device, encrypt outputs locally, and forward encrypted payloads
into the existing `relay.py` flow without introducing a new trust boundary. This solves a gap in
browser-only workflows for local model execution, reduces desktop app footprint versus Electron,
and provides tighter runtime permissions for a client that needs native process control plus
privacy-preserving transport. Tauri is the recommended path because token.place needs a thin UI
shell around native inference and crypto forwarding, not a full Node-centric desktop platform.

## 2) Problem statement

Browser clients are excellent for remote API usage, but they are a poor default for local-model
operator workflows that need process lifecycle control, GPU backend selection, filesystem model
management, resilient local logging, and explicit offline behavior. token.place needs a dedicated
desktop client so local inference is first-class while preserving the current encrypted client ↔
relay ↔ server model.

The existing Electron direction in `desktop/` is not the right long-term default for this target
shape. It is currently a legacy prototype centered on tray + idle scheduling and launching
`server.py` (`desktop/src/main/Main.ts`, `desktop/src/main/IdleScheduler.ts`) rather than a
purpose-built local LLM inference + encrypted forwarding architecture.

## 3) Goals

- Ship a local-first desktop shell for token.place local LLM workloads.
- Run local CPU/GPU inference via a native llama.cpp-backed runtime boundary.
- Encrypt prompt/output artifacts locally before they leave the device.
- Forward encrypted results through existing `relay.py` contracts where possible.
- Keep install footprint and idle memory lower than a typical Electron baseline.
- Enforce explicit permission boundaries (filesystem, process, network).
- Establish a cross-platform path for macOS, Linux, and Windows.
- Support future UX features: token streaming, cancellation, logs, model selection, progress.

## 4) Non-goals

- Replacing the existing browser client.
- Rewriting `relay.py` or `server.py` in this design phase.
- Introducing a brand-new protocol when current token.place contracts can be reused.
- Building a full marketplace/operator management desktop suite in v1.
- Fully solving all signing/packaging/GPU edge cases in phase one.

## 5) Why Tauri instead of Electron

Balanced comparison for token.place:

- **Bundle size / memory / startup**
  - Tauri generally ships a smaller app because it uses system WebView + Rust host.
  - Electron bundles Chromium + Node runtime, typically increasing installer size and idle RAM.
- **Security posture**
  - Tauri has capability-scoped APIs and explicit allowlist patterns for commands/plugins.
  - Electron can be secured well, but often requires stricter discipline around preload/IPC and
    Node integration hardening.
- **Sidecar process support**
  - Both can launch helper processes. Tauri sidecar patterns map cleanly to a llama.cpp boundary.
- **Ecosystem strengths for Electron**
  - Electron still has a mature Node package ecosystem and larger historical desktop app examples.
  - If token.place required deep Node-native desktop integrations across many modules, Electron
    could still be a better fit.
- **Why token.place favors Tauri now**
  - The intended shape is: desktop UI shell + native inference runtime + local encryption + relay
    forwarding. Tauri aligns with this by keeping the host thin and permissioned while inference
    remains in a dedicated native process boundary.

## 6) Proposed architecture

Recommended boundaries:

- **Tauri frontend UI (WebView)**
  - Task composer, model/runtime settings, stream viewer, retry controls, diagnostics.
- **Tauri Rust command layer**
  - Controlled commands for sidecar lifecycle, settings persistence, secure key access, and relay
    submissions.
- **Inference sidecar**
  - llama.cpp-backed helper process (see Section 7) emitting structured events.
- **Local storage**
  - Model path metadata, runtime settings, and encrypted optional history.
- **Secure key storage**
  - OS keychain-backed storage for long-lived client key material; ephemeral session material in
    memory only.
- **IPC / streaming**
  - Event channel from sidecar to UI for tokens/progress/errors; cancellation token path from UI to
    sidecar.
- **Local encryption + forwarding**
  - Encrypt locally, then POST encrypted payloads to relay-compatible endpoints.

```text
+----------------------- token.place desktop (Tauri) ------------------------+
| UI (WebView) <-> Rust commands <-> Sidecar manager <-> llama.cpp sidecar |
|        |                 |                    |                             |
|        |                 +-> key store        +-> token stream events       |
|        +-> render stream / cancel                                          |
|                              |                                              |
|                    local encryption module                                  |
+------------------------------|----------------------------------------------+
                               v
                        relay.py (ciphertext only)
                               v
                      server.py / provider path
```

Relationship to existing abstractions:

- Reuse current encrypted payload structure and relay/server expectations where stable.
- Keep crypto compatibility with existing Python/JS flows (see `encrypt.py` and
  `utils/crypto_helpers.py`) while choosing a desktop-native implementation boundary.

## 7) Inference runtime strategy

Options:

1. **`llama-server` sidecar binary**
   - Pros: existing HTTP surface, easier rapid integration, less custom protocol work.
   - Cons: additional local HTTP layer and translation overhead.
2. **Custom wrapper sidecar around llama.cpp**
   - Pros: structured event protocol can be shaped for token.place needs (stream chunks,
     cancellation, model diagnostics) without exposing extra local APIs.
   - Cons: more implementation work and maintenance burden.
3. **Direct library integration in Tauri host**
   - Pros: fewer moving processes.
   - Cons: tighter coupling, harder crash isolation, more complex host binary concerns.

Recommendation: start with a **sidecar boundary** (option 1 or 2), prefer a custom wrapper if
structured streaming/cancellation semantics are needed early. Keep it replaceable.

Important: GPU acceleration is provided by llama.cpp backend builds (Metal/CUDA/Vulkan/CL/ROCm),
not by Tauri itself. Tauri only orchestrates sidecar launch, args, and telemetry.

Required behaviors:

- Token streaming as structured events (`token`, `progress`, `done`, `error`).
- Cancellation via explicit command and sidecar ack.
- Parse stdout/stderr into bounded diagnostic buffers.
- Fallback to CPU when GPU backend is unavailable.
- Clear error taxonomy for missing/incompatible model files and backend mismatch.

## 8) Security and privacy model

Threat model focus:

- Protect prompt/output plaintext from relay and server visibility during transit.
- Minimize local plaintext lifetime and accidental leakage via logs/crash dumps.
- Limit desktop app capabilities to least privilege.

Security rules:

- Plaintext exists only in active UI/session memory and inference-side process memory while running.
- Encrypt response artifacts before network egress to relay.
- relay.py sees ciphertext + metadata required for routing, not decrypted content.
- Store long-lived keys in OS credential storage (Keychain/DPAPI/libsecret via vetted plugin).
- Disable broad shell execution and arbitrary filesystem access; allowlist model directories.
- Logging policy: no plaintext prompt/response in default logs; optional debug mode must be
  explicit, local-only, and redacted.
- Job history default: off or encrypted-at-rest opt-in.

## 9) Platform support strategy

Recommended rollout:

1. **macOS (Apple Silicon) first**: predictable local-LLM demand, strong Metal path.
2. **Windows next**: high user base, but CUDA packaging/signing complexity is higher.
3. **Linux third**: fragmented distros and mixed Vulkan/ROCm/NVIDIA packaging realities.

Support labels initially:

- **Supported**: tested installer + smoke/E2E + documented backend path.
- **Experimental**: best-effort builds with partial test coverage and known caveats.

## 10) Packaging and distribution

- Package app via Tauri bundler per platform.
- Bundle sidecar binaries per target architecture or download verified sidecar artifacts on first
  run.
- Plan for code signing/notarization:
  - macOS: Developer ID + notarization (Gatekeeper).
  - Windows: code signing cert to reduce SmartScreen warnings.
  - Linux: signed release artifacts where feasible.
- Model strategy in v1: bring-your-own-model path plus optional guided download with checksum
  verification.
- Updates: staged auto-update once signing pipeline is stable.
- Expect AV false positives for packed inference binaries; document hash verification.

## 11) UX flows

- **First launch**: detect platform/backend support, explain local-first/privacy model.
- **Model setup**: select existing GGUF file or guided import/download.
- **Runtime selection**: Auto/CPU/GPU mode with backend capability detection.
- **Inference run**: submit task, stream tokens live, allow cancel/retry.
- **Encrypt + forward**: user confirms forwarding; app encrypts locally and submits to relay path.
- **Relay unreachable**: show retry/backoff and optional local queue policy status.
- **Sidecar crash**: classify error, surface restart action, preserve unsent local work where safe.
- **Model/backend incompatibility**: actionable fix guidance (quantization/backend mismatch).
- **Diagnostics view**: bounded logs, environment snapshot, copy-safe redacted report.

## 12) Integration boundaries

Define explicit contracts:

- **UI ↔ sidecar**: typed request/response + event stream schema.
- **Desktop ↔ relay.py**: reuse existing encrypted transport contract and envelope shape where
  compatible.
- **Reuse candidates**: message envelope conventions from current crypto/client modules.
- **Future shared artifacts**: versioned schema docs for stream events and encrypted relay payloads.
- **Topology choice**: desktop can talk directly to relay.py by default; optional localhost helper
  abstraction is acceptable later if needed for retries/queueing.

## 13) Migration / retirement plan from Electron

Current repo state:

- `desktop/package.json` defines an Electron app with `electron-builder` packaging scripts.
- `desktop/src/main/Main.ts` and `desktop/src/main/IdleScheduler.ts` are focused on tray/idle
  orchestration and spawning `server.py`.
- `docs/REPO_MAP.md` and `docs/CROSS_PLATFORM.md` include Electron-forward language today.

Decision:

- Mark current Electron desktop implementation as **legacy/deprecated prototype** now.
- Freeze feature development on Electron path.
- Keep code for short-term historical reference only.
- Retire/archive/remove in a later implementation PR once Tauri parity milestones are met.

Immediate cleanup in this PR:

- Add this design doc as forward-looking architecture.
- Update repo docs to stop presenting Electron as active direction.
- Add explicit deprecation note in `desktop/README.md` linking to this design.

Follow-up tasks (future PRs):

- Scaffold `desktop-tauri/` or `apps/desktop-tauri/` with minimal shell.
- Introduce typed sidecar protocol and smoke tests.
- Add migration issue checklist for eventual Electron removal.

Risk if stale Electron docs/scripts remain:

- Contributors may keep investing in deprecated path.
- CI/release effort can fragment across two desktop strategies.

## 14) Risks and open questions

- Should v1 use `llama-server` sidecar or a custom wrapper?
- Where should crypto live for desktop: Rust-native, reused JS, reused Python helper, or mixed?
- How should large encrypted payloads be chunked and retried?
- Should forwarding be immediate-only in v1 or include encrypted offline queueing?
- What model management UX is acceptable before adding downloads/caching/pinning?
- Is subprocess isolation permanent, or should tighter integration be considered later?
- What telemetry/observability is acceptable while preserving privacy defaults?

## 15) Phased implementation plan

- **Phase 0**: design approval + Electron deprecation messaging (this PR).
- **Phase 1**: Tauri shell, settings, placeholder sidecar wiring, smoke tests.
- **Phase 2**: local llama.cpp inference + streaming and cancellation UX.
- **Phase 3**: local encryption + relay.py forwarding integration.
- **Phase 4**: packaging/signing/notarization and platform hardening.
- **Phase 5**: model management, resilience (queue/retry), advanced diagnostics UX.

## 16) Test strategy

Planned test layers:

- Unit tests for command handlers, settings validation, and event parser logic.
- UI ↔ sidecar integration tests (streaming, cancellation, crash recovery).
- Crypto compatibility tests against existing token.place encrypted envelope expectations.
- Relay contract tests verifying encrypted forwarding through `relay.py` behavior.
- GPU fallback tests (backend unavailable, incompatible build, low VRAM fallback to CPU).
- End-to-end smoke tests on at least one supported platform per release candidate.
- CI alignment: add desktop-tauri checks without regressing existing `pre-commit` and
  `run_all_tests.sh` expectations.
