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
| Dependency isolation | Desktop imports resolve from the desktop target/import root and never from user site packages or repo-local shims. | Same isolation inside `.app/Contents/Resources`. | Same isolation for CPU-only installs. | Packaged bridge e2e, private runtime-origin checks, `interpreter`, `prefix`, and import-root diagnostics. |
| Packaged resource resolution | Packaged app resolves the shared bridge, model bridge, runtime setup, requirements, and config files. | `.app/Contents/Resources` resolves the same shared files and launcher paths. | Same paths remain valid without GPU libraries. | `test_packaged_operator_e2e.py` logs. |
| Warm-load before register | Bridge warms the relay-processing runtime before API v1 registration. | Same sequence. | Same sequence. | Bridge logs contain `model_init.ready` with `reason=pre_registration` before `api_v1_e2ee.register`. |
| Relay registration | Registered only after warm-load and runtime readiness. | Same behavior. | Same behavior when CPU runtime is explicitly ready. | `/relay/diagnostics` and bridge status show `registered: true`. |
| Multi-relay prod+staging registration | One operator can register the same warmed model with both `https://token.place` and `https://staging.token.place`. | Same behavior. | Same behavior when CPU runtime is explicitly accepted for the release claim. | Desktop status shows a registered count such as `2/2`; production and staging `/relay/diagnostics` each show the expected safe node metadata. |
| Multi-turn API v1 E2EE chat | Multiple encrypted `/api/v1/chat/completions` turns complete without plaintext relay state. | Same behavior. | Same behavior. | Client decrypts responses; relay logs show request queued, response received, response retrieved. |
| Per-relay failure isolation | A staging or production relay outage/error does not kill the other relay registration, polling loop, or local warmed runtime. | Same behavior. | Same behavior. | Stop one relay target or use an invalid stopped-only URL for one target, then verify the healthy relay stays registered/polling and can complete a landing chat. |
| Stopped-only relay URL editing | Relay URL changes are blocked while the operator is running and apply only after Stop then the next Start. | Same behavior. | Same behavior. | UI evidence shows Relay URL fields disabled while running, no live relay mutation occurs, and edited prod+staging URLs are used only by the next operator session. |
| Stop | Stop cancels polling, unregisters when possible from every configured relay, exits bridge, and reports `registered: false`. | Same behavior. | Same behavior. | Desktop status and all configured `/relay/diagnostics` targets show no stale node, or document TTL expiry if a relay is unreachable during unregister. |
| Start after Stop | A fresh session starts, warms, registers, and answers another encrypted turn; stale old-session events do not overwrite status. | Same behavior. | Same behavior. | New `operator_session_id` and successful post-restart turn. |
| Two-node round-robin participation | Two Windows-capable nodes can register and participate according to relay scheduling. | Two macOS-capable nodes can register and participate according to relay scheduling. | CPU-only nodes participate only when explicitly accepted for the release claim. | Staging diagnostics show two nodes, queue depth returns to zero, and per-node assignment/response logs demonstrate rotation. |

Do not make production two-node or round-robin claims until both Windows and macOS release candidates pass this checklist, including GPU-capable runtime evidence for the platform-specific release artifacts. For v0.1.x release validation, the shared checklist also requires prod+staging multi-relay evidence: a single stopped-only configuration must include `https://token.place` and `https://staging.token.place`, the operator must report both relay registrations (for example `2/2`), one landing chat must complete through each relay, a partial relay failure must not stop the other relay registration/poll loop, and Stop must unregister from every configured relay or record safe TTL-expiry evidence.

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
- Packaged Apple Silicon releases use the bundled Python runtime; Command Line Tools are development-only and not an end-user prerequisite.
- Metal-enabled install/repair uses `CMAKE_ARGS=-DGGML_METAL=on -DGGML_NATIVE=off`, `FORCE_CMAKE=1`, and the repo-pinned `llama-cpp-python` version.
- Validate the packaged `.app` path as well as the development path so `.app/Contents/Resources` uses the same bridge/runtime code as Windows packaged builds. The local packaged e2e covers a fake `.app/Contents/Resources` layout with mock Metal registration and a bounded `gpu` failure path; release sign-off still requires manual Apple Silicon validation with a real Metal-capable runtime.
- Capture packaged debug logs from app stdout/stderr and preserve `desktop.runtime_setup` plus bridge registration lines. Public runtime setup/status payloads should show safe fields such as `interpreter`, `python_version`, `prefix`, `base_prefix`, `dependency_target`, `pip_version`, `runtime_action`, safe origin booleans/categories, and any bounded pip/CMake tails from provisioning. The internal runtime handoff serializes the versioned SHA-256 `llama_module_identity`, never the raw absolute `llama_module_path`; raw paths must not be copied into bridge events or public diagnostics.
- If the bundled runtime is missing or invalid in a packaged app, reinstall token.place desktop or use a newer release; do not ask end users to install Python or Xcode Command Line Tools.

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
python desktop-tauri/scripts/run_desktop_parity_checks.py

# Broad PR operator validation is Linux/Windows only; it includes Python 3.9
# inspect coverage on Ubuntu plus simulated macOS Contents/Resources and fake
# Metal assertions that do not require native Darwin behavior.
gh workflow run desktop-operator-e2e.yml

# Native macOS app launch/shutdown coverage is isolated to a small Apple Silicon
# smoke workflow on main pushes, a daily schedule, or manual dispatch.
gh workflow run desktop-macos-smoke.yml
```

Linux simulated-macOS parity deliberately does **not** validate signing, DMG
mounting, Mach-O metadata, native process launch semantics, or real Metal
offload. Those remain native macOS/release concerns.

### Local hardware runtime checks

```powershell
# Windows PowerShell on real Windows 11 NVIDIA hardware.
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model C:\path\to\model.gguf
python desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py --mode gpu --context-tier 64k-full --model "$env:APPDATA\token.place\models\Qwen3-8B-Q4_K_M.gguf"
```

The smoke helper must observe the safe local phase sequence: `dependency_check`,
optional `dependency_install`/`lock_wait`, `runtime_probe`, optional
`runtime_install` or `cuda_build`, `runtime_verification`, optional `reexec`,
`model_preflight`, `warm_load`, then ready/registered. Long phases should produce
5-second heartbeats. A fast path may skip build/reexec phases; a repair path should
include them. Public bridge/status payloads keep absolute module paths private and
use safe origin indicators such as `llama_repo_stub_imported=false`; the helper
performs runtime-origin validation internally. Fake-CUDA CI validates contracts only
and is not evidence of a successful real Windows 11 NVIDIA packaged run.

```bash
# macOS shell on Metal-capable hardware before a release.
CMAKE_ARGS=-DGGML_METAL=on FORCE_CMAKE=1 python -m pip install --force-reinstall --no-cache-dir llama-cpp-python
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf
python desktop-tauri/scripts/test_desktop_no_relay_autostart_e2e.py
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

Packaged macOS launches do not attach to a developer terminal by default, so the
compute-node operator persists each Start session to an app-local debug log. The
Tauri status includes `log_file_path`, and the desktop UI exposes opt-in buttons
for:

- **Open debug log**: opens an in-app read-only console showing the current log
  tail with a copy button.
- **Copy log path**: copies the current session log path for support notes or shell commands.
- **Reveal log file**: reveals the log in Finder (or the equivalent file manager
  on other platforms).
- **Open debug terminal**: opens a terminal tailing the current log. On macOS the
  app uses Terminal.app with a read-only `tail -n 200 -F` command and quotes the
  log path so spaces in `~/Library/Logs/...` work.

The macOS log is created under the app log directory in an `operator/` subfolder,
with timestamped per-session names like `compute-node-<operator_session_id>-<timestamp>.log`. The log starts with the
operator session id, sanitized relay target, requested mode, bridge path,
interpreter, resource root, packaged layout, and import root. It then mirrors
bridge stdout/stderr, `desktop_runtime_setup` probe/provision output,
`model_manager` warm-load output, registration/poll/request/response lifecycle
lines, and stop/cancel/unregister cleanup lines.

For manual validation, set `TOKEN_PLACE_DESKTOP_OPEN_DEBUG_TERMINAL=1` before
launching the packaged app if you want the terminal tail to open automatically
when Start operator creates a session log. Normal packaged launches keep terminal
access opt-in through the UI.

When Start operator fails on macOS, capture:

1. The UI **Last error** field.
2. The **Operator debug log** path shown in the UI.
3. The first `desktop.compute_node.session.start` and
   `desktop.compute_node.session.layout` lines.
4. Any public `desktop_runtime_setup`, runtime-origin, `interpreter`, `prefix`,
   `metal`, `llama_cpp`, `model_init`, registration, unregister, cancel, or
   bridge stderr lines from the log.

Debug logs must remain operator diagnostics only. Do not add plaintext prompts,
responses, tool arguments, model-output text, private keys, or decrypted relay
payloads to persisted logs.

## macOS packaged Metal runtime validation

Packaged Apple Silicon `.app` builds must include their own Python runtime at
`Contents/Resources/python-runtime/bin/python3`. Users installing the release DMG
do not need Python, Homebrew, Xcode, Xcode Command Line Tools, CMake, compiler
toolchains, or Python packages. A missing-runtime message means the app bundle is
incomplete or damaged and should be fixed by reinstalling token.place desktop or
using a newer release.

Manual validation on Apple Silicon:

1. Build and install the macOS desktop release from the commit under test.
2. Confirm `Contents/Resources/python-runtime/bin/python3` exists and
   `embedded_python_runtime_provenance.json` is present.
3. Run release-artifact validation with a sanitized environment and no
   `TOKEN_PLACE_PYTHON` / `TOKEN_PLACE_SIDECAR_PYTHON` overrides.
4. Start the operator with `mode=auto`, staging relay URL, and a local GGUF model.
5. Confirm runtime probing reports Metal, GPU offload support,
   `llama-cpp-python==0.3.32`, and the Qwen 64K YaRN constructor capabilities.
6. Confirm `model_init.ready` appears before API v1 registration and verify the UI
   shows `Registered: yes`.

Development builds may still use an explicit Python 3.11+ override for local
iteration, but packaged release validation must not invoke `/usr/bin/python3`,
Homebrew Python, pyenv, or Command Line Tools Python.
