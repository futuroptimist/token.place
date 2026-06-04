# 2026-06-04 packaged desktop llama_cpp import timeout

## Summary
Packaged desktop compute-node startup on Windows 11 could fail before relay registration even when
`desktop_runtime_setup` had already found a known-good CUDA `llama-cpp-python` runtime. The operator
stopped after `llama_cpp_import_timeout after 30s` and never reached `Registered: yes`.

## Symptoms
- Packaged bridge launched from the expected desktop resources path, including paths containing
  spaces such as `token.place desktop`.
- Runtime setup reported `selected_backend=cuda`, `device=cuda`, and `runtime_action=already_supported`.
- Warm-load logged `Locating llama_cpp runtime for model initialization...` and discovered the
  installed `llama_cpp` module quickly with `find_spec`.
- A separate `llama_cpp import watchdog` subprocess then timed out after 30 seconds while running
  `importlib.import_module('llama_cpp')`.
- The bridge failed closed and surfaced an error, but the operator never became registered with the
  relay and the UI remained `Registered: no`.

## Timeline
1. Original regression: packaged desktop operator startup could hang at
   `Locating llama_cpp runtime for model initialization...` before relay registration.
2. Failed fix attempt: bounded discovery/import watchdogs improved diagnostics and converted the
   unbounded hang into `llama_cpp_import_timeout after 30s`, but startup still failed.
3. This PR: removes the child-process pre-import watchdog from the startup-critical path, keeps
   direct bridge-process import semantics, and strengthens registration lifecycle tests.

## Why the prior fix was insufficient
The watchdog imported `llama_cpp` in a child process before the bridge imported it for actual model
initialization. On packaged Windows/macOS native runtimes, that child can have subtly different cwd,
`PYTHONPATH`, bootstrap environment, DLL/search-path state, extended-path spelling, stdout/stderr
behavior, or native CUDA/Metal initialization timing. A timeout in that subprocess therefore became
an accidental startup blocker even though the runtime setup probe had already validated the installed
runtime path.

## Why CI/e2e missed it
Existing packaged checks covered dependency preflight, inspect flows, mock LLM paths, bridge JSON
startup, and relay polling, but did not sufficiently guard the real packaged lifecycle invariant that
a warm runtime must either reach `registered=true` / UI `Registered: yes` or fail cleanly with an
actionable bounded error. In particular, mock and inspect paths could pass without proving the
startup-critical model-manager import path matched the successful desktop runtime setup probe.

## Cross-platform risk
Although the reproduced failure was Windows 11 CUDA, the broken seam was shared packaged runtime
bootstrap/model-manager import logic. macOS Metal and CPU-fallback packaged launches use the same
bridge/model-manager path, so a child pre-import watchdog could also diverge from the actual
bridge-process runtime environment there.

## Corrective actions in this PR
- Removed the startup-critical child-process `llama_cpp` pre-import watchdog from model warm-load.
- Kept direct import in the actual bridge process so model initialization uses the same interpreter,
  `sys.path`, cwd, and native-library environment as the running operator.
- Preserved bounded session behavior through the existing warm-load/pre-registration deadline rather
  than adding another import subprocess.
- Normalized Windows `\\?\` resource/import-root strings before adding Python import roots while
  preserving paths with spaces and macOS `.app/Contents/Resources` layouts.
- Suppressed relay unregister attempts when no fresh registration was ever confirmed.

## Prevention tests added
- Unit coverage that model-manager startup no longer calls the import watchdog before direct parent
  import, including no-SIGALRM platforms.
- Unit coverage for Windows extended-path import roots with spaces and macOS `.app/Contents/Resources`.
- Unit coverage that runtime/bridge shutdown skips unregister before a confirmed fresh registration.
- Existing packaged operator e2e continues to require `registered=true` and fails if the bridge stays
  `Running: yes / Registered: no`.

## Manual validation checklist
- Build and install a Windows desktop release from this PR.
- Start the operator against staging with the known-good CUDA GGUF and `auto` mode.
- Confirm logs include `llama_cpp runtime located`, `Llama init completed successfully`,
  `model_init.ready`, and `server.registered`, with no `llama_cpp_import_timeout`.
- Confirm UI shows `Running: yes`, `Registered: yes`, CUDA backend fields, and no last error.
- Repeat packaged macOS validation; it should reach `Registered: yes` when runtime support is
  available or fail cleanly with bounded actionable Metal/runtime diagnostics.
