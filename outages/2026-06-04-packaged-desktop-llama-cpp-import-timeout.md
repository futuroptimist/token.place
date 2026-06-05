# 2026-06-04 packaged desktop llama_cpp import timeout and facade early exit

## Summary
Packaged desktop compute-node startup could fail before relay registration even when
`desktop_runtime_setup` had already found a supported Windows CUDA `llama-cpp-python` runtime.
The operator moved from warm-load to failed/stopped and never reached `Registered: yes`.

## Symptoms
- Original regression: logs stopped after `Locating llama_cpp runtime for model initialization...`.
- First failed fix: discovery diagnostics completed quickly, but a separate child import watchdog
  timed out with `llama_cpp_import_timeout after 30s`.
- Second failed fix: warm-load reused the desktop probe and selected CUDA, but the no-SIGALRM
  subprocess runtime facade exited before its first import/model-init handshake. The parent only
  reported the legacy generic `llama_cpp_import subprocess ended`, then `model_init.failed`, with no
  `server.registered` event and no UI `Registered: yes`.

## Timeline
- Original regression: packaged desktop operator warm-load hung while locating the `llama_cpp`
  runtime, so relay registration and UI `Registered: yes` never happened.
- Failed fix attempt 1: bounded subprocess discovery/import watchdogs were added. Discovery via
  `find_spec` completed quickly, but the separate child-process `import llama_cpp` watchdog still
  timed out after 30 seconds.
- Failed fix attempt 2: startup skipped the parent import on Windows/background warm-load threads
  and returned a subprocess-backed `llama_cpp` facade. That got past discovery, but the facade child
  could exit before emitting its JSON handshake and the parent suppressed the child stderr/stdout,
  exit code, cwd, import root, and module-path hint.
- Corrective fix: kept the bounded subprocess facade for no-SIGALRM runtimes, but made it faithful to
  the successful runtime setup import path and made early child exit actionable.

## Root cause
The runtime setup probe and the startup-critical runtime facade did not have an explicit, shared
bootstrap contract. Packaged Windows paths such as `token.place desktop` and Tauri `\\?\` extended
paths could flow into child `PYTHONPATH`/import-root state differently than the successful desktop
probe. The facade also imported `llama_cpp` before entering its JSON error-reporting block and sent
stderr to `DEVNULL`, so import-time child failures collapsed to an unhelpful EOF message.

## Why the prior fixes were insufficient
The first watchdog fix created a second native-extension import environment before the real bridge
warm-load environment. The second facade fix avoided poisoning the parent process on Windows, but it
still launched a second child environment without surfacing enough details to tell whether cwd,
`PYTHONPATH`, `TOKEN_PLACE_PYTHON_IMPORT_ROOT`, `TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT`, DLL search
state, or the probed `llama_cpp` module path diverged from runtime setup.

## Why CI/e2e missed it
Existing packaged coverage validated bridge imports, dependency preflight, mock-LLM bridge events,
and inspect-style paths. It did not force the packaged bridge lifecycle to fail when the operator
never reached `registered=true` on the runtime-backed path, and it did not simulate the exact facade
failure shape where runtime setup succeeds but the facade child exits before its import handshake.
Mock runtime mode could still reach bridge registration without exercising real `llama_cpp` startup
sequencing.

## Cross-platform risk
The reproduced failure was Windows 11 CUDA, but the seam is shared packaged runtime/path bootstrap
code. macOS Metal packaged apps use the same bridge/runtime setup/model-manager import handoff and
could have hit an equivalent child import divergence, especially around `.app/Contents/Resources`
packaged roots.

## Corrective actions in this PR
- Reuse the successful `desktop_runtime_setup` probe module path during model warm-load.
- Normalize Windows `\\?\` paths before placing packaged import roots and bootstrap script paths in
  child subprocess environments while preserving spaces in paths such as `token.place desktop`.
- Keep repo-local `llama_cpp.py` shim protection and the desktop probe/import path consistency check.
- Move facade import/model initialization inside the child JSON error-reporting block.
- Replace the legacy generic EOF marker with the actionable `llama_cpp_import subprocess exited before JSON handshake` diagnostic including exit code, stdout tail, stderr tail, command, cwd, import root, module-path hint, and stage.
- Intentionally keep `TOKEN_PLACE_LLAMA_CPP_JSON` protocol stdout payloads out of diagnostic tails so prompts, completion chunks, and generated text are not logged by the parent.
- Keep relay unregister skipped unless API v1 registration succeeded.

## Prevention tests added
- Unit coverage for stripping Windows extended path prefixes from runtime-worker env state.
- Unit coverage for facade child early exit including exit code, stdout tail, stderr tail, import
  root, module-path hint, paths with spaces, initial stdin write failures, and protocol stdout
  redaction.
- Packaged e2e regression coverage that simulates successful runtime setup plus a facade child that
  exits before its handshake and asserts the failure is actionable rather than a false lifecycle
  success.
- Packaged standard and macOS `Contents/Resources` lifecycle checks continue to fail unless the
  bridge reports `registered=true` and `relay_runtime_state=ready`.
