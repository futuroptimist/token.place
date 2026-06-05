# 2026-06-04 packaged desktop llama_cpp import timeout and facade startup failure

## Summary
Packaged desktop compute-node startup could fail before relay registration even when
`desktop_runtime_setup` had already found a supported Windows CUDA `llama-cpp-python` runtime.
The operator moved from warm-load to failed/stopped and never reached `Registered: yes`.

## Symptoms
- Original regression: logs stopped after `Locating llama_cpp runtime for model initialization...`.
- First attempted fix: discovery finished, but a child import watchdog timed out with
  `llama_cpp_import_timeout after 30s` before registration.
- Second attempted fix: warm-load got past runtime discovery by using desktop probe diagnostics and
  a subprocess runtime facade, but `Llama(...)` failed almost immediately with only the generic
  parent error `llama_cpp_import subprocess ended`.
- The unregister side effect was fixed separately: when no API v1 registration succeeded, stop now
  skips relay unregister instead of mutating relay state for a node that never registered.

## Timeline
- Original regression: packaged desktop operator warm-load hung while locating the `llama_cpp`
  runtime, so relay registration and UI `Registered: yes` never happened.
- Failed fix attempt 1: bounded subprocess discovery/import watchdogs were added. Discovery via
  `find_spec` completed quickly, but the separate child-process `import llama_cpp` watchdog still
  timed out after 30 seconds.
- Failed fix attempt 2: the warm-load path skipped parent import and returned a subprocess-backed
  `llama_cpp` facade. That bounded the old hang, but the facade worker could exit before its first
  protocol message and the parent collapsed the whole failure to `llama_cpp_import subprocess ended`.
- Current corrective fix: keep the killable facade for no-`SIGALRM` startup paths, but make its
  environment match the desktop runtime probe, normalize packaged Windows extended paths before they
  reach child env/import paths, capture stderr/stdout/exit-code diagnostics on early child exit, and
  catch worker import failures inside the JSON protocol so the parent receives actionable failures.

## Root cause
The startup-critical child processes did not faithfully share the same bootstrap contract as the
successful desktop runtime probe. The Windows packaged bridge can be launched with `\\?\` extended
paths and an import root containing spaces (for example `token.place desktop`). The runtime probe
normalizes those paths, sets packaged bootstrap env, and proves that CUDA `llama_cpp` imports from
site-packages. The subprocess facade worker instead relied on a separate `python -c` bootstrap and
previously discarded child stderr. If the worker failed while importing `llama_cpp`, resolving the
packaged import root, or loading native dependencies, it could exit before any JSON handshake and the
parent would only report a generic subprocess-ended error.

A second diagnostic gap made the failure non-actionable: the worker imported `llama_cpp` before the
`try` block that emits JSON protocol errors, and the parent launched it with `stderr=DEVNULL`.
Import-time exceptions therefore disappeared.

## Why the prior fixes were insufficient
The watchdog and the first facade implementation both introduced another runtime environment in
front of the real model initialization path. On packaged Windows/macOS native-extension runtimes, a
child can differ from the bridge and the desktop probe in `sys.path`, cwd, `PYTHONPATH`,
`TOKEN_PLACE_PYTHON_IMPORT_ROOT`, `TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT`, `PYTHONNOUSERSITE`,
`PATH`/DLL lookup, and `\\?\` path handling. A startup-critical path must either import in the
already-proven bridge environment or make the child environment equivalent and observable.

## Why CI/e2e missed it
Existing packaged coverage validated bridge imports, dependency preflight, mock-LLM bridge events,
and inspect-style paths. Mock runtime mode could still reach bridge registration without exercising
real `llama_cpp` import/model initialization sequencing. The fake-runtime packaged lifecycle test
also did not assert all of the user-visible lifecycle milestones that matter for this outage:
warm-load success, `model_init.ready`, relay registration, and a status event with
`registered=true` / `relay_runtime_state=ready`.

## Cross-platform risk
The reproduced failure was Windows 11 CUDA, but the seam is shared packaged runtime/path bootstrap
code. macOS Metal packaged apps use the same bridge/runtime setup/model-manager import handoff and
could hit equivalent child import divergence, so macOS `Contents/Resources` packaged parity remains
part of the regression suite.

## Corrective actions in this PR
- Reuse the successful `desktop_runtime_setup` probe module path during model warm-load.
- Keep the subprocess runtime facade killable for no-`SIGALRM` warm-load paths, but align its env
  with the desktop runtime probe: normalized `TOKEN_PLACE_PYTHON_IMPORT_ROOT`, desktop bootstrap
  env, `PYTHONNOUSERSITE=1`, sanitized `PYTHONPATH`, and normalized `PATH` entries.
- Avoid passing Windows `\\?\` extended prefixes through the facade worker env/sys.path while still
  preserving robust path comparisons for repo-local shim rejection.
- Capture early facade worker failures with exit code, stderr tail, stdout tail, command/program,
  cwd, import root, module-path hint, and stage name.
- Catch worker `llama_cpp` import and model-init exceptions inside the JSON protocol and surface the
  traceback tail to the parent instead of losing it.
- Keep the no-unregister-before-registration behavior.

## Prevention tests added
- Unit coverage that facade early exit includes child exit code, stderr/stdout tails, import root,
  command/cwd/module-path context, and normalized paths with spaces.
- Unit coverage that the facade worker env follows the desktop probe bootstrap contract and strips
  unsafe `\\?\` prefixes from env/PYTHONPATH/PATH.
- Unit coverage that JSON protocol worker errors include traceback details.
- Packaged fake-runtime lifecycle checks now require warm-load success, `model_init.ready`, relay
  registration, `registered=true`, and `relay_runtime_state=ready`; they reject the old timeout and
  generic subprocess-ended markers.
- Existing standard and macOS `Contents/Resources` packaged lifecycle checks continue to fail unless
  `registered=true` appears.
