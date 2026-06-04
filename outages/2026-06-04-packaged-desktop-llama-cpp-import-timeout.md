# 2026-06-04 packaged desktop llama_cpp import timeout

## Summary
Packaged desktop compute-node startup could fail before relay registration even when
`desktop_runtime_setup` had already found a supported Windows CUDA `llama-cpp-python` runtime.
The operator moved from warm-load to failed/stopped and never reached `Registered: yes`.

## Symptoms
- Logs stopped after `Locating llama_cpp runtime for model initialization...` in the original
  regression.
- A later diagnostic-only fix made the hang bounded, but startup still failed with
  `llama_cpp_import_timeout after 30s`.
- Windows packaged logs showed runtime setup selecting CUDA and locating the real
  `site-packages/llama_cpp/__init__.py`, followed by a child import watchdog timeout before model
  initialization.

## Timeline
- Original regression: packaged desktop operator warm-load hung while locating the `llama_cpp`
  runtime, so relay registration and UI `Registered: yes` never happened.
- Failed fix attempt: bounded subprocess discovery/import watchdogs were added. Discovery via
  `find_spec` completed quickly, but the separate child-process `import llama_cpp` watchdog still
  timed out after 30 seconds.
- This PR: removed the startup-critical child import watchdog, reused successful desktop runtime
  probe diagnostics, and kept the real import in the bridge process that initializes the model.

## Why the prior fix was insufficient
The watchdog imported `llama_cpp` in a subprocess before the actual bridge process imported it.
On packaged Windows/macOS native-extension runtimes, that child can have different `sys.path`, cwd,
`PYTHONPATH`, `PYTHONNOUSERSITE`, import-root, DLL/search-path, and extended-path behavior than the
runtime setup probe or the real bridge warm-load process. The watchdog therefore became a second
runtime environment that could fail differently from the environment that needed to start.

## Why CI/e2e missed it
Existing packaged coverage validated bridge imports, dependency preflight, mock-LLM bridge events,
and inspect-style paths, but it did not faithfully simulate the broken child-watchdog import shape.
Mock runtime mode could still reach bridge registration without exercising real `llama_cpp` import
sequencing, so CI did not prove that a packaged desktop + relay lifecycle would reject
`Running: yes / Registered: no` when the model warm-load path stalled before registration.

## Cross-platform risk
The reproduced failure was Windows 11 CUDA, but the seam was shared packaged runtime/path bootstrap
code. macOS Metal packaged apps use the same bridge/runtime setup/model-manager import handoff and
could have hit an equivalent child import divergence.

## Corrective actions in this PR
- Reuse the successful `desktop_runtime_setup` probe module path during model warm-load.
- Remove the startup-critical pre-import child watchdog from the real bridge startup path.
- Import `llama_cpp` directly in the bridge process and verify that it matches the desktop probe
  module path when a probe was available.
- Normalize Windows `\\?\` paths for comparisons/import-root resolution without using extended
  paths as the preferred packaged import-root representation.
- Skip relay unregister when no API v1 registration succeeded.

## Prevention tests added
- Unit coverage for reusing desktop runtime probe diagnostics and proving the child import watchdog
  is not called in the startup-critical path.
- Unit coverage for Windows extended paths with spaces and macOS-style resource paths.
- Packaged e2e regression coverage with a fake `llama_cpp` that would hang only in the removed child
  watchdog context, plus existing standard and macOS `Contents/Resources` packaged lifecycle checks
  that fail unless `registered=true` appears.
