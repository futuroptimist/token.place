# Outage: packaged desktop llama_cpp import timeout before registration

- **Date:** 2026-06-04
- **Slug:** `packaged-desktop-llama-cpp-import-timeout`
- **Affected area:** packaged desktop operator startup on Windows CUDA and equivalent macOS runtime paths

## Summary
Packaged desktop compute-node startup could warm-load forever or fail before relay registration while
showing `Running: yes` and `Registered: no`. The reproduced Windows 11 CUDA case located a known-good
`llama-cpp-python` install during desktop runtime setup, but model warm-load then timed out in a
separate `llama_cpp` import watchdog subprocess and never reached `server.registered` / UI
`Registered: yes`.

## Symptoms
- Logs stopped at or after `Locating llama_cpp runtime for model initialization...`.
- A follow-up diagnostic build progressed to `llama_cpp runtime discovery complete`, then failed with
  `llama_cpp_import_timeout after 30s` from the import watchdog subprocess.
- The bridge transitioned to failed/stopped cleanly, but the operator never registered with the relay.
- Stop could attempt unregister even when no registration had succeeded, producing confusing relay
  errors.

## Timeline
1. Original regression: packaged desktop operator warm-load hung at llama_cpp runtime location during
   pre-registration startup.
2. Failed fix attempt: bounded subprocess discovery/import watchdogs improved observability but made
   the watchdog import a startup-critical gate; the watchdog still timed out on Windows CUDA.
3. This PR: model warm-load now reuses the successful desktop runtime probe module path and imports
   `llama_cpp` directly in the real bridge process instead of pre-importing it in a second watchdog
   environment.

## Root cause
Desktop runtime setup and model warm-load used different import environments. Runtime setup had
already proven the packaged interpreter could import/probe the CUDA or Metal runtime, but model
warm-load repeated discovery and required a separate child-process `import llama_cpp` before the real
model process imported it. On native GPU builds, that duplicate child import can diverge because of
packaged `sys.path`, cwd, `PYTHONPATH`, user-site suppression, DLL/native runtime initialization, and
Windows extended-path handling.

## Why the prior fix was insufficient
The prior fix bounded the symptom but did not remove the divergent import path. It converted an
unbounded hang into an actionable `llama_cpp_import_timeout after 30s`, but the packaged operator was
still blocked before `llama_cpp runtime located`, model init, relay registration, and UI
`Registered: yes`.

## Why CI/e2e missed it
Existing packaged checks emphasized importability, inspect-only paths, mock LLM behavior, and bridge
JSON lifecycle events. They did not consistently fail the packaged desktop lifecycle when the bridge
remained in a running-but-unregistered state, and they did not model the Windows/macOS packaged
runtime seam where runtime setup succeeds but model warm-load uses a different import strategy.

## Cross-platform risk
The reproduced machine was Windows 11 CUDA, but the broken seam was shared packaged runtime bootstrap
code. macOS `.app/Contents/Resources` Metal/CPU-fallback layouts use the same bridge/model-manager
path policy and could suffer the same pre-registration warm-load divergence.

## Corrective actions in this PR
- Reuse successful desktop runtime setup diagnostics during model initialization.
- Remove the startup-critical `llama_cpp` import watchdog from the warm-load path.
- Import `llama_cpp` once in the real bridge process after shared path sanitization and shim guards.
- Normalize Windows extended path prefixes for packaged Python bootstrap comparisons without breaking
  spaces in resource paths.
- Suppress relay unregister attempts unless registration previously succeeded.

## Prevention tests added
- Unit coverage that model warm-load reuses the desktop runtime probe module path and skips child
  rediscovery when available.
- Unit coverage that no-SIGALRM platforms import in the bridge process instead of through the
  watchdog/subprocess facade.
- Packaged e2e coverage now requires `registered=true` with `relay_runtime_state=ready` and fails on
  running-but-unregistered lifecycle gaps.
- Path-bootstrap coverage for Windows extended paths with spaces and macOS packaged resource parity.
