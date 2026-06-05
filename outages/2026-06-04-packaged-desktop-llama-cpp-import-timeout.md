# 2026-06-04 packaged desktop llama_cpp import timeout

## Summary
Packaged desktop compute-node startup could fail before relay registration even when
`desktop_runtime_setup` had already found a supported Windows CUDA `llama-cpp-python` runtime.
The operator moved from warm-load to failed/stopped and never reached `Registered: yes`.

## Symptoms
- Logs stopped after `Locating llama_cpp runtime for model initialization...` in the original
  regression.
- A first diagnostic-only fix made the hang bounded, but startup still failed with
  `llama_cpp_import_timeout after 30s` from a child import watchdog.
- A second fix skipped the parent warm-load import and introduced a subprocess runtime facade. That
  got as far as compute-plan selection and `Llama init started`, then failed almost immediately with
  the generic parent error `llama_cpp_import subprocess ended` and no relay registration.
- Windows packaged logs showed runtime setup selecting CUDA and locating the real
  `site-packages/llama_cpp/__init__.py`, but the startup-critical worker was still using a second
  subprocess environment that could exit before the JSON handshake.

## Timeline
- Original regression: packaged desktop operator warm-load hung while locating the `llama_cpp`
  runtime, so relay registration and UI `Registered: yes` never happened.
- Failed fix attempt 1: bounded subprocess discovery/import watchdogs were added. Discovery via
  `find_spec` completed quickly, but the separate child-process `import llama_cpp` watchdog still
  timed out after 30 seconds.
- Failed fix attempt 2: the import watchdog was avoided by returning a subprocess-backed
  `llama_cpp` facade on Windows/background warm-load threads. The facade child inherited an
  inconsistent/opaque startup contract and discarded stderr, so early child exits collapsed to
  `llama_cpp_import subprocess ended`.
- This PR: made the retained facade faithful to the runtime/probe import path, normalized packaged
  `\\?\` paths before child subprocess use, moved child import into the JSON error path, and surfaced
  exit code/stdout/stderr/cwd/command/import-root/module-path diagnostics when the child exits before
  handshake.

## Root cause
The startup-critical model initialization path had two subtly different runtime contracts:
`desktop_runtime_setup` proved that the selected interpreter and native `llama_cpp` package could
support CUDA/Metal, while the later watchdog/facade child process was launched with its own cwd,
`PYTHONPATH`, `sys.path`, import-root, and Windows extended-path values. When that second child
failed before its protocol handshake, stderr was discarded and the parent emitted only a generic
subprocess-ended error. Paths containing spaces and `\\?\` prefixes made this especially hard to
reason about on packaged Windows installs.

## Why the prior fixes were insufficient
The watchdog and then the facade imported `llama_cpp` in a subprocess before or instead of the actual
bridge process import. On packaged Windows/macOS native-extension runtimes, that child can have
different `sys.path`, cwd, `PYTHONPATH`, `PYTHONNOUSERSITE`, import-root, DLL/search-path, and
extended-path behavior than the runtime setup probe or the real bridge warm-load process. The facade
also read only stdout protocol messages and sent stderr to `DEVNULL`, so a Python traceback, DLL load
failure, or bootstrap failure was hidden.

## Why CI/e2e missed it
Existing packaged coverage validated bridge imports, dependency preflight, mock-LLM bridge events,
and inspect-style paths, but it did not faithfully simulate the broken facade child exiting before
its JSON handshake. Mock runtime mode could still reach bridge registration without exercising the
real `llama_cpp` warm-load/facade failure shape, so CI did not prove that a packaged desktop + relay
lifecycle would reject `Running: yes / Registered: no` when model warm-load failed before
registration.

## Cross-platform risk
The reproduced failure was Windows 11 CUDA, but the seam was shared packaged runtime/path bootstrap
code. macOS Metal packaged apps use the same bridge/runtime setup/model-manager import handoff and
could have hit an equivalent child import divergence or hidden pre-registration failure.

## Corrective actions in this PR
- Reuse the successful `desktop_runtime_setup` probe module path during model warm-load.
- Keep the startup-critical pre-import child watchdog out of the real bridge startup path.
- Make the retained subprocess facade use the same sanitized import path contract as runtime probes,
  with Windows `\\?\` prefixes stripped from child env/PYTHONPATH values while preserving spaces.
- Capture facade child stderr/stdout tails and report exit code, command/program, cwd, import root,
  module-path hint, stage, and traceback/error details on early exit.
- Catch child import/model-init exceptions inside the worker protocol so failures are returned as
  JSON instead of disappearing before handshake when possible.
- Continue to skip relay unregister when no API v1 registration succeeded.

## Prevention tests added
- Unit coverage for facade early exit diagnostics, including stderr tail, stdout tail, exit code,
  command/cwd/import-root, and module-path hint.
- Unit coverage for Windows extended paths with spaces in runtime-worker env/PYTHONPATH handling.
- Packaged e2e coverage that simulates a successful runtime-probe shape followed by a facade child
  exit before handshake and asserts the lifecycle cannot be considered registered.
- Existing standard and macOS `Contents/Resources` packaged lifecycle checks continue to fail unless
  `registered=true` appears.
