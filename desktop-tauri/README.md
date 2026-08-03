# token.place desktop Tauri MVP

This folder contains the forward-looking Tauri desktop MVP for token.place.

Functional changes included in a rebuilt desktop installer normally require a patch-version bump;
major/minor bumps remain maintainer-directed. Update all synchronized package, Tauri, Cargo,
installer-validation, and fixture versions, then validate consistency with
`python -m pytest -q tests/unit/test_desktop_release_artifacts.py` from the repository root. See the
canonical [desktop versioning policy](../AGENTS.md#desktop-versioning-policy). A bump does not create
a tag or publish a release.

## Scope of this MVP

- Single-screen UI with a background compute-node operator mode plus a local prompt smoke-test panel.
- Shows the canonical model family page and runtime GGUF artifact metadata from
  shared Python config/runtime logic.
- Lets users either browse to an existing GGUF or download the configured GGUF
  artifact into the shared models directory.
- Runtime backend preference controls expose `auto`, `metal`, `cuda`, and `cpu` modes.
- Background compute-node mode that warm-loads the local runtime, registers through API v1 E2EE relay routes, polls for encrypted work, runs local inference, and submits encrypted non-streaming responses.
- Sidecar-driven local prompt smoke-test output with explicit cancellation.
- Shared Windows/macOS operator lifecycle behavior for warm-load, register, multi-turn chat, Stop, Start after Stop, and diagnostics.

## Compute-node bridge behavior

Desktop includes a Python compute-node bridge
(`src-tauri/python/compute_node_bridge.py`) that reuses shared runtime and API v1 E2EE relay logic for desktop compute-node work. The bridge runs as the primary operator path and emits status events (running/registered, active relay URL, backend mode, model path, warm-load state, runtime backend fields, and last error).
Root `server.py` remains the canonical non-desktop compute-node entrypoint; desktop behavior must stay parity-aligned with the shared API v1 E2EE relay contract and must not reintroduce legacy relay endpoints or API v1 streaming.

See [Desktop parity validation checklist](../docs/desktop_parity_validation.md) for the evergreen Windows/macOS release checklist, expected UI fields by lifecycle state, staging commands, queue-depth checks, Stop/Start checks, and two-node round-robin evidence requirements.

The fake sidecar remains available at `sidecar/fake_llama_sidecar.py` for CI
and fast local testing:

- Set `TOKEN_PLACE_USE_FAKE_SIDECAR=1` to explicitly opt into the fake sidecar.
- Set `TOKEN_PLACE_SIDECAR=/full/path/to/script.py` to explicitly override the
  sidecar command path (this takes precedence over
  `TOKEN_PLACE_USE_FAKE_SIDECAR`).

## Run locally

```bash
cd desktop-tauri
npm ci
npm run tauri dev
```

`tauri dev` runs the UI against your system Python/sidecar; it does **not** package the
embedded runtime, so it cannot exercise packaging-only bugs (embedded-runtime PATH handling,
packaged import-root resolution, bundled resources). To build a real local installer instead,
see [Build a local installer](#build-a-local-installer-fast-iteration-loop) below.

During normal startup, desktop sidecars probe the active sidecar interpreter and, in GPU-capable modes, use platform-specific runtime bootstrap/repair where supported (Windows CUDA and macOS Metal) while preserving shared bridge lifecycle behavior. They emit:

- `desktop.runtime_setup ...` during sidecar start (backend selected + fallback reason)
- `compute_runtime ...` after `Llama(...)` init (backend actually used, offloaded
  layers, KV cache placement, and fallback reason)

Set `TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP=1` to explicitly disable runtime bootstrap and keep startup in probe-only mode (useful for packaging/troubleshooting while preserving fallback diagnostics).

When GPU runtime repair is needed, desktop uses the same interpreter binary that launches the sidecar process (`sys.executable`) and applies the repo-pinned `llama-cpp-python` source-build recipe with the platform flag:

- Windows CUDA: `CMAKE_ARGS=-DGGML_CUDA=on`
- macOS Metal: `CMAKE_ARGS=-DGGML_METAL=on`
- Both: `FORCE_CMAKE=1` and `pip install llama-cpp-python==<repo-pinned-version> --force-reinstall --no-cache-dir --verbose`

After a successful repair, the sidecar automatically re-execs once so the active process immediately uses the repaired runtime (no manual restart/environment flag required).

### Platform runtime expectations

- **macOS Apple Silicon**: validate with a Metal-enabled llama.cpp sidecar build and packaged `.app/Contents/Resources` resource resolution.
- **Windows 11 + NVIDIA GPU**: validate with a CUDA-enabled llama.cpp sidecar build.
- CPU fallback mode is available in both cases, with explicit fallback details surfaced in `desktop.runtime_setup ... fallback_reason=...` and `compute_runtime ... fallback_reason=...`. Missing `llama_cpp` is a dependency failure, not silent CPU fallback.
- `backend_available` reports runtime capability, `backend_selected` reports policy selection, and `backend_used` reports the initialized relay-processing runtime. GPU release claims depend on `backend_used`.

## Build a local installer (fast iteration loop)

`.github/workflows/desktop-release.yml` (the canonical release build) takes roughly 45
minutes per push, since it builds both macOS and Windows in a matrix. To validate packaging
changes without that round trip, build a real local installer with one command:

```bash
python3 desktop-tauri/scripts/build_local.py
```

(equivalently, `make desktop-build` from the repo root). This reproduces the same sequence
CI runs — Rust target setup, `npm ci`, embedded Python runtime prep, `npm run build`,
`npm run tauri build`, and artifact staging/validation — and writes the result to
`desktop-tauri/release-artifacts/`:

- **macOS**: a `.app` (ad-hoc signed unless `APPLE_SIGNING_IDENTITY` /
  `APPLE_CERTIFICATE_P12_BASE64` / `APPLE_CERTIFICATE_PASSWORD` are already set in your
  environment, same as CI) and a `.dmg` built via `hdiutil`.
- **Windows 11**: both a NSIS `*-setup.exe` and a `*.msi` (Tauri's bundler downloads NSIS/WiX
  itself on first run — no separate install needed). Requires the MSVC C++ build tools
  (Visual Studio "Desktop development with C++" workload or the standalone Build Tools) for
  the Rust linker; WebView2 ships with Windows 11 already.

This must run on the target OS itself — there is no cross-compilation from macOS to Windows
or vice versa.

**Launch the installed app, not the raw build output.** `classify_python_execution_layout()`
in `src-tauri/src/python_runtime.rs` treats an executable running from beneath
`src-tauri/target/` *while the source tree's `python/desktop_runtime_setup.py` is also
present alongside it* as unbundled development, not a packaged run — by design, since
`cargo build` output always lands under `target/` inside the checkout. Running the `.app`
straight out of `src-tauri/target/.../bundle/macos/` (or the NSIS/MSI output before
installing) while still inside the git checkout silently skips the bundled-runtime
resolution path entirely and falls back to a system Python interpreter instead — which is a
different code path than what ships. To actually exercise the packaged runtime resolution
locally, install/copy the built app **out of the repo** first: open the `.dmg` and drag to
`/Applications` on macOS, or run the NSIS/MSI installer on Windows, then launch it from
there.

Useful flags:
- `--dry-run` — print the exact command sequence without running anything.
- `--skip-install` — skip `npm ci` (e.g. `node_modules` is already current).
- `--skip-validate` — skip the post-build artifact validation step.
- `--fresh-runtime` — force re-running the embedded Python runtime prep. On Windows this
  matters: unlike the macOS prep script, the Windows one has no "already valid" short-circuit
  and always re-downloads the pinned CUDA wheel + native DLLs, so by default the wrapper
  skips it once `src-tauri/python-runtime/python.exe` already exists. That's what turns
  repeat local builds into a fast recompile loop instead of paying that download every time.

The desktop-release CI pins **Node 20** and **Python 3.11**, which differ from the repo
root's `.nvmrc` (18) and `.python-version` (3.12) — make sure the right versions are active
(e.g. `nvm use 20`) before running the build.

Local output is a preview build only: unsigned or ad-hoc signed, not notarized, matching what
CI produces without Apple Developer ID / Windows code-signing secrets configured. It's for
validating the packaged runtime path locally, not for distribution.

**Recovering from an interrupted build.** A machine losing power or restarting mid `cargo
build`/`cargo fetch` can leave a corrupted Cargo registry cache or partially-written `.rmeta`
build artifacts, which then fail on the next build with confusing errors like `key with no
value, expected =` or `found invalid metadata files` / `corrupt metadata encountered` rather
than a clean "try again". If you hit that, wipe the local build artifacts and retry:

```bash
python3 desktop-tauri/scripts/clean_local_build.py --all
```

(equivalently, `make desktop-clean`). With no flags it only removes fast-to-rebuild
project-local output (`src-tauri/target/`, `src-tauri/gen/`, `release-artifacts/`, `dist/`);
`--all` (or `--node-modules` / `--runtime` / `--cargo-registry` individually) also clears
`node_modules`, the embedded Python runtime, and the **global** `~/.cargo/registry` cache
(shared across every Rust project on the machine, not just this repo — that's why it's
opt-in). `--dry-run` previews what would be removed.

## Privacy defaults

- Prompt/response plaintext stays in-memory by default.
- The app only persists non-plaintext settings (model path, relay URLs,
  preferred mode) in app-local config.
- Relay URLs default to `https://token.place`, remain user-editable while the operator is stopped,
  and migrate automatically from the legacy single Relay URL config field.
- Log lines are redacted to metadata (byte counts, request ids).


## Multi-relay compute-node operation

The Compute node operator panel supports multiple relay URLs for v0.1.x desktop nodes. Use this when
one machine should serve both production and staging during release validation.

The operator also has a stopped-only **Context tier** selector. Choose **8K Fast** for
`n_ctx=8192` or **64K Full** for `n_ctx=65536` before clicking **Start operator**.
The selected tier is saved in the desktop config and is applied during warm-load before relay
registration; changing it requires **Stop operator** followed by **Start operator** so only one
context profile is warm in each operator process.

On a capable Metal or CUDA runtime, **64K Full** normally uses symmetric Q8_0 for both the K and V
KV caches, with Flash Attention and KQV offload enabled. F16 is retained as the compatibility
fallback, while symmetric Q4_0 is attempted only for a positively classified memory or GPU-buffer
pressure failure and may have a larger quality tradeoff. This cache precision is independent of
the Q4_K_M quantization of the model weights. Quantized V cache is never enabled without confirmed
Flash Attention and matching K/V type support. Batch values remain `n_batch=256` and
`n_ubatch=128`; later batch tuning and packaged performance/quality benchmarking are separate work,
so this change does not claim an unmeasured speedup.
This context tier selector only chooses and warm-loads the operator runtime context window. It intentionally
does not change API v1 request admission, relay request-size policy, relay scheduling, or registration
capabilities; long-context admission and tier-aware selection remain follow-up work so the API v1
E2EE relay contract stays unchanged in this patch.

1. Stop the operator before editing relay URLs. Relay URL fields are stopped-only, and changes apply
   on the next Start operator action.
2. Add one URL per field with **Add new relay URL**. Blank entries are ignored when saved or started.
3. For prod+staging validation, configure:
   - `https://token.place`
   - `https://staging.token.place`
4. Start the operator and wait for the shared llama.cpp runtime to warm. For v0.1.x, one node can
   serve both prod and staging because API v1 exposes one model: `qwen3-8b-instruct`.
5. Confirm status shows the configured relay URLs and a registered count such as `2/2` when both
   relays accept registration. Status entries use relay labels/counts only; docs and examples must
   not include full public keys.

Partial failures are isolated per relay. If one relay URL is unreachable, unauthorized, or blocked by
network policy, the other relay poll/register loop continues and the status count reflects only the
healthy registrations. Fix the failed route, then restart if needed so the changed stopped-only relay
configuration is applied cleanly.

When the operator stops, desktop cancels polling and attempts to unregister from every configured
relay. Operators should confirm each relay diagnostics page drops or expires the registration:

- `https://token.place/relay/diagnostics`
- `https://staging.token.place/relay/diagnostics`

Relay and desktop logs must remain relay-blind: ciphertext only plus safe routing metadata, without
plaintext prompts, responses, private keys, decrypted payloads, or full public keys.

## Cutting a desktop release

Desktop binaries released as GitHub Release assets are published only by the canonical GitHub Actions workflow
`Desktop Tauri Release` (`.github/workflows/desktop-release.yml`).

1. Create an explicit desktop tag on the commit you want to release:
   ```bash
   git tag desktop-v0.1.0 <commit-sha>
   git push origin desktop-v0.1.0
   ```
2. GitHub Actions builds `desktop-tauri/` artifacts on macOS and Windows and
   uploads them to the GitHub Release named `desktop-v0.1.0`.
   - For Apple Silicon Macs (M1/M2/M3/M4), the release asset is intentionally
     named `token.place-desktop-<version>-apple-silicon.dmg` so users can pick
     the correct file quickly.
   - Desktop Tauri release staging is Tauri-only (`desktop-tauri/src-tauri/target/.../bundle`);
     legacy Electron artifacts from `desktop/` must never be published on this
     release channel.
   - If Apple signing credentials are not configured in CI, the workflow emits
     an explicit preview warning and uses ad-hoc signing for dev/preview builds.
     Those builds are not equivalent to fully Developer ID signed + notarized
     Gatekeeper-ready releases.
   - If signing credentials are configured, CI validates with `codesign`.
     Strict Gatekeeper notarization checks are skipped unless notarization is added.

### Unpaid macOS preview releases

- token.place can publish Apple Silicon preview DMGs without a paid
  Apple Developer Program account.
- These preview builds use ad-hoc signing and are not notarized, so Gatekeeper
  warnings after browser/GitHub download are expected.
- The mounted DMG now includes an inline opening guide (`README BEFORE OPENING.txt`)
  plus a sidecar release asset (`README-macos-apple-silicon-preview.txt`) that
  explain the same manual-open flow.
- Users must manually open/whitelist trusted previews:
  - Drag/copy `token.place desktop.app` to **Applications** and try opening once.
  - If macOS blocks with the expected “Apple could not verify…” dialog, click
    **Done**, then use **System Settings → Privacy & Security** to
    **Open Anyway / Allow / Open**.
  - Control-click (or right-click) and choose **Open** remains a fallback path.
- This flow does not remove Gatekeeper warnings for unpaid preview releases; it
  explains the expected manual allow/open steps.
- No-warning public distribution generally requires paid
  Developer ID signing plus notarization.

You can also run the workflow manually with `workflow_dispatch` and provide
`tag_name` to rebuild/re-publish an existing desktop tag.


The local prompt panel still uses `src-tauri/python/inference_sidecar.py` as a smoke test for local model setup.


## Desktop logging defaults

Desktop subprocess logs now default to high-signal output:

- Known low-value llama.cpp noise (metadata dumps, control-token spam, per-layer assignment spam, tensor repack spam) is filtered at the Tauri stderr forwarding boundary.
- Warnings and errors are always preserved in default mode.
- The inference sidecar emits concise stderr summary lines for model init and prompt/eval throughput.

To restore full raw subprocess output (including verbose llama.cpp logs), run desktop with either:

- `TOKEN_PLACE_VERBOSE_LLM_LOGS=1`
- `TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS=1`

Both flags are equivalent opt-in toggles and intended for troubleshooting.


### Manual runtime verification

Use the helper below to print authoritative runtime wiring details from the same
Python environment used by desktop sidecars:

```bash
python desktop-tauri/scripts/verify_desktop_runtime.py --mode auto --model /path/to/model.gguf
```

The verifier exits non-zero if `llama_module_path` points at the repo-local shim
(`.../token.place/llama_cpp.py`). A healthy runtime should resolve
`llama_module_path` to the installed `llama-cpp-python` package path (for
example `.../site-packages/llama_cpp/__init__.py`).
When this shadowing is detected, the stable runtime action is
`shadowed_repo_llama_cpp` in both verifier and smoke-test diagnostics.

It prints:

- Shared runtime probe fields (`backend`, `gpu_offload_supported`,
  `detected_device`, `interpreter`, `prefix`, `llama_module_path`)
- `compute_runtime_*` summaries using stable fields
  (`requested`, `effective`, `backend_available`, `backend_used`,
  `device_backend`, `device_name`, `offloaded_layers`, `kv_cache`,
  `fallback_reason`, `interpreter`, `llama_module_path`)

### Regression and smoke tests

- Shared local desktop parity entry point (packaged resources + API v1 E2EE relay lifecycle):
  ```bash
  python desktop-tauri/scripts/run_desktop_parity_checks.py
  ```
- Operator startup regression coverage (bridge startup event + surfaced errors):
  ```bash
  pytest -q --noconftest tests/unit/test_desktop_compute_node_bridge.py
  npm --prefix desktop-tauri run test -- src/App.test.tsx
  ```
- Real Windows 11 + NVIDIA GPU viability smoke test (same desktop Python/runtime path):
  ```powershell
  python desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py --installer C:\path\to\token-place-setup.exe --mode gpu --context-tier 64k-full --model "$env:APPDATA\token.place\models\Qwen3-8B-Q4_K_M.gguf"
  ```
  Pass means the canonical Qwen3 artifact matches its filename, size, and SHA-256; the installed
  Tauri executable starts the Rust-managed operator through the UI; and status proves a concrete
  NVIDIA device, bundled runtime, CUDA selection/use, positive offload, non-CPU KV cache, and warm
  load. The gate completes a non-mock encrypted API-v1 turn, clicks Stop, observes relay count zero,
  and requires ordered unregister success followed by a natural bridge exit. Direct bridge launch
  or cancellation, `physical_device_missing`, CPU fallback, repo shims, and mock inference fail.

## Packaged operator debug logs

The packaged desktop app writes compute-node operator diagnostics to a per-session
log file in the app log directory. On macOS this is typically under the user's
Library logs area in an `operator/` subdirectory, for example:

```text
~/Library/Logs/<tauri app identifier>/operator/compute-node-<operator_session_id>-<timestamp>.log
```

The Compute node operator panel shows the current `Operator debug log` path after
Start operator creates a session. Use **Open debug log** for an in-app read-only
console with copy support, **Copy log path** to copy the current session path,
**Reveal log file** to open the containing location, or **Open debug terminal** to tail the log. macOS terminal tailing uses
Terminal.app and a quoted `tail -n 200 -F` command so paths with spaces work.

For validation runs that should open the terminal automatically when the operator
starts, launch the app with:

```bash
TOKEN_PLACE_DESKTOP_OPEN_DEBUG_TERMINAL=1
```

Capture the debug log when Start operator fails. Important startup details include
`desktop.compute_node.session.start`, `desktop.compute_node.session.layout`, the
selected interpreter/resource/import roots, `desktop_runtime_setup` probe and
provision lines, bridge stdout/stderr, model warm-load diagnostics,
registration/poll/request/response lifecycle lines, and stop/cancel/unregister
cleanup lines. Logs intentionally avoid plaintext prompts, responses, private
keys, and decrypted relay payloads.

### macOS packaged Metal runtime provisioning

Apple Silicon release DMGs bundle a self-contained CPython 3.11 runtime at
`token.place desktop.app/Contents/Resources/python-runtime/bin/python3`. Normal
packaged-app users do not need Python, Homebrew, Xcode, Xcode Command Line
Tools, CMake, compiler toolchains, or Python packages. Packaged macOS startup
uses only the bundled runtime unless the user deliberately sets the documented
development override variable.

If the bundled runtime is missing, damaged, incompatible, or blocked, the app
fails closed with a concise reinstall message and a stable diagnostic code. Do
not ask packaged-app users to install Python or developer tools for that case;
reinstall the desktop app or use a newer release artifact. Developer builds may
still use `TOKEN_PLACE_PYTHON` / `TOKEN_PLACE_SIDECAR_PYTHON` to point at a
Python 3.11+ interpreter.

When the bundled runtime is valid, runtime probing reports `runtime_origin=bundled`
and must not run pip, reinstall `llama-cpp-python`, require network access, or
silently fall back from Metal to CPU. Repair packages, when explicitly needed,
remain in the writable app-data dependency target and are installed by invoking
pip through the bundled interpreter rather than modifying `Contents/Resources`.

### Qwen 64K batch profiles

The packaged operator offers three deterministic profiles for the **64K Full** context tier:

| Profile | `n_batch` | `n_ubatch` | Use |
| --- | ---: | ---: | --- |
| Balanced (default) | 512 | 256 | Recommended initial product setting |
| Safe | 256 | 128 | Conservative P4-equivalent setting |
| Experimental | 1024 | 512 | Explicit opt-in with greater memory and stability risk |

Changing this preference requires stopping and restarting the operator. The preference is retained
but not applied for the 8K tier. On recognized memory pressure, Q8 and F16 profiles downshift from
Experimental to Balanced to Safe before the Safe Q4 memory fallback. Q8 compatibility failures
instead use F16 at the current batch size; arbitrary backend or decode failures do not trigger a
batch or Q4 fallback. Performance varies by backend and hardware. P8 will add formal comparative
benchmarks and regression thresholds; these values are not performance guarantees.
