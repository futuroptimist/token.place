# Outage: desktop-tauri Windows GPU runtime stayed on CPU after CUDA repair attempts

- **Date:** 2026-04-18
- **Slug:** `desktop-tauri-windows-gpu-runtime-cpu-fallback`
- **Affected area:** desktop-tauri local inference/operator GPU modes on Windows 11 + NVIDIA

## Summary
Desktop GPU and hybrid modes could repeatedly fall back to CPU even on Windows hosts with NVIDIA GPUs and CUDA-capable drivers/tooling.

## Impact
- Desktop users could select `auto`, `gpu`, or `hybrid` and still run fully on CPU.
- Throughput/latency remained CPU-bound.
- Runtime logs suggested bootstrap activity, but the installed runtime often remained CPU-only.

## Root cause
The wheel-based runtime repair/install path did **not** pass `--force-reinstall` to `pip install`.
When a CPU-only `llama-cpp-python` of the same version was already present (common in packaged installs),
pip treated the requirement as satisfied and skipped replacing it with the CUDA wheel, leaving the runtime CPU-only.

## Resolution
- Updated desktop GPU packaging install arguments to always include `--force-reinstall` so wheel repair paths can replace a preinstalled CPU wheel with a GPU-capable build of the same version.
- Added/updated tests that assert force-reinstall behavior in both packaging helper contracts and runtime bootstrap command construction.
- Added a high-level e2e contract test covering Windows bootstrap sequencing, ensuring CUDA reinstall is attempted and can promote runtime action to CUDA reexec before CPU fallback.

## Follow-up / prevention
- Keep GPU install/repair commands aligned with the README hardware-acceleration guidance (`--force-reinstall`, `--upgrade`, `--no-cache-dir`).
- Preserve test coverage around Windows repair sequencing to avoid future regressions that silently keep CPU wheels in place.
