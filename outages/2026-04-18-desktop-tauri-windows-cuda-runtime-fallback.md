# Outage: desktop-tauri Windows CUDA runtime repeatedly fell back to CPU

- **Date:** 2026-04-18
- **Slug:** `desktop-tauri-windows-cuda-runtime-fallback`
- **Affected area:** desktop-tauri Windows 11 operator/local inference GPU flows

## Summary
Desktop Windows users with NVIDIA GPUs saw `mode=auto`/`mode=gpu` sessions start in CPU mode.
Runtime bootstrap frequently reported CPU fallback after install attempts, reducing throughput and
invalidating expected desktop GPU parity.

## Symptoms
- `desktop.runtime_setup ... selected_backend=cpu ... fallback_reason=...`
- `compute_node_bridge.py` startup/status events reported `backend_used=cpu` and
  `offloaded_layers=0`.
- Packaged runtime repair logs frequently mentioned missing `requirements.txt` and unpinned
  reinstall paths.

## Impact
- Desktop operators on Windows 11 + NVIDIA often ran CPU-only inference despite GPU hardware.
- GPU confidence for desktop release artifacts dropped because CI lacks direct NVIDIA validation.

## Root cause
Two packaging/bootstrap gaps combined:

1. **Packaged desktop resources did not include `requirements.txt`.**  
   Runtime repair could not resolve the pinned `llama-cpp-python` version and fell back to
   unpinned install behavior.
2. **Windows install plan prioritized wheel-only CUDA attempts before source-compiled CUDA.**  
   This increased the chance of landing on CPU fallback paths when CUDA wheel compatibility
   mismatched Python ABI/version constraints.

## Resolution
- Bundled `requirements.txt` into desktop Tauri resources so packaged runtime repair can use the
  repo-pinned `llama-cpp-python` contract.
- Updated runtime setup to search packaged resource layouts for `requirements.txt` before defaulting
  to unpinned behavior.
- Updated Windows desktop install plan ordering to try **CUDA source build first** (`CMAKE_ARGS`
  + `FORCE_CMAKE`, `--no-binary llama-cpp-python`), then pinned/unpinned CUDA wheel fallbacks, then
  CPU fallback as a last resort.
- Added regression and E2E-style contract tests to lock plan ordering and packaged-resource
  expectations.

## Follow-up / prevention
- Keep README hardware-acceleration instructions mirrored in AGENTS guidance for desktop/runtime
  contributors.
- Treat CPU fallback in `mode=gpu`/`mode=auto` as a release-risk signal for Windows desktop builds.
- Continue running `desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py` on real Windows +
  NVIDIA hardware before desktop release publication.
