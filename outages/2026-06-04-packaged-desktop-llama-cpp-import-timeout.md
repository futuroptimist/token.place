# Packaged desktop llama_cpp import timeout blocked compute-node registration

- **Date:** 2026-06-04
- **Status:** Corrective action added
- **Area:** Packaged desktop operator runtime bootstrap (Windows CUDA, macOS Metal/CPU parity)

## Symptoms

Packaged desktop compute-node startup could hang or fail before relay registration. The UI remained effectively `Running: yes / Registered: no`, and logs stopped near `Locating llama_cpp runtime for model initialization...` or later failed with `llama_cpp_import_timeout after 30s`.

## Timeline

1. A packaged desktop regression left the operator stuck while locating or importing the `llama_cpp` runtime during API v1 pre-registration warm-load.
2. A follow-up added subprocess discovery/import watchdog diagnostics. Discovery (`find_spec`) completed quickly and reported the installed `llama-cpp-python` module, but the new child-process import watchdog still timed out after 30 seconds on a known-good Windows 11 CUDA runtime.
3. The bridge transitioned to failed/stopped instead of hanging forever, but it still never reached `model_init.ready`, `server.registered`, or UI `Registered: yes`.
4. This PR removes the startup-critical child import watchdog path, reuses the packaged runtime setup probe for compute planning, and keeps pre-registration warm-load bounded by the bridge session deadline.

## Root cause

The model warm-load path imported `llama_cpp` in a separate watchdog subprocess after desktop runtime setup had already proven the runtime import/probe path. That second process used a different import environment (`PYTHONPATH`, cwd, probe env vars, and packaged path normalization) from the actual bridge runtime. On native CUDA/Metal builds, that duplicate child import could hang or diverge even when the real runtime was usable.

Windows `\\?\` extended paths and spaces in packaged resource paths increased the risk that import-root comparison and child process path contracts differed from the runtime setup probe. macOS `.app/Contents/Resources` uses the same shared bootstrap seam, so the prevention must be platform-neutral.

## Why prior coverage missed it

Existing packaged desktop e2e coverage primarily exercised mock LLM and import-inspection paths. Those tests proved that the bridge launched and dependencies were importable, but they did not require a non-mock packaged runtime warm-load to complete and report `registered=true`/`Registered: yes`. As a result, a real packaged desktop + relay lifecycle could remain in `Running: yes / Registered: no` without failing CI.

## Corrective actions in this PR

- Removed the startup-critical `llama_cpp` pre-import watchdog from model initialization.
- Kept direct import in the actual bridge process, using the same sanitized runtime path rules as desktop runtime setup.
- Preserved the broader pre-registration warm-load deadline so genuine runtime stalls fail closed with an actionable `Last error` instead of leaving the operator half-registered.
- Reused desktop runtime setup diagnostics for compute planning to avoid repeated fragile CUDA/Metal probing.
- Normalized Windows extended path prefixes before adding packaged import roots while preserving spaces in resource paths and macOS `.app/Contents/Resources` resolution.
- Suppressed relay unregister attempts when no relay registration has succeeded.

## Prevention tests added

- Unit coverage for no-SIGALRM direct import without invoking the child import watchdog.
- Unit coverage for Windows extended-path packaged import roots with spaces.
- Relay unregister coverage that no-ops before successful API v1 registration.
- Packaged operator e2e coverage that requires `registered=true` for both standard resources and macOS `Contents/Resources` layouts.
- A fake non-mock `llama_cpp` packaged runtime guard that would stall if the removed child-watchdog import environment is reintroduced, ensuring CI catches the previous failure shape without requiring CUDA hardware.

## Manual validation checklist

After merge, build Windows and macOS packaged releases from HEAD. On Windows 11 with the known-good CUDA `llama-cpp-python` runtime, verify logs progress through `llama_cpp runtime located`, `Selected compute plan`, `Llama init completed successfully`, `model_init.ready`, and `server.registered`; verify UI `Registered: yes`, no `llama_cpp_import_timeout`, staging diagnostics show one node with `queue_depth=0`, chat returns model text, Stop unregisters/TTL-expires the node, and Start reaches `Registered: yes` again. Repeat macOS packaged validation and require either `Registered: yes` with an available runtime or a bounded actionable Metal/runtime error.
