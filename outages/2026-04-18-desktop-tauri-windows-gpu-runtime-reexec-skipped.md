# Outage: desktop-tauri Windows GPU runtime repair re-exec was skipped in operator mode

- **Date:** 2026-04-18
- **Slug:** `desktop-tauri-windows-gpu-runtime-reexec-skipped`
- **Affected area:** desktop-tauri operator path (`compute_node_bridge.py`) on Windows 11 + NVIDIA

## Summary
Desktop operator mode (`compute_node_bridge.py`) could still run CPU-only inference on Windows even
after a successful CUDA runtime repair flow. The bridge disabled the runtime refresh re-exec path,
so it did not guarantee the updated GPU-capable `llama-cpp-python` build was active for the running
process.

## Symptoms
- `desktop.runtime_setup` logs indicated repair activity but operator startup often still reported
  CPU backend usage.
- Desktop operator `started` events could show `backend_used=cpu` and `offloaded_layers=0` after
  repair attempts.
- Windows + NVIDIA operator throughput/latency remained CPU-bound.

## Impact
Windows 11 operators with NVIDIA GPUs could not reliably benefit from GPU offload in desktop
operator mode, even though local runtime repair logic existed. This reduced performance and made
GPU readiness difficult to validate with confidence.

## Root cause
`compute_node_bridge.py` called:

- `maybe_reexec_for_runtime_refresh(runtime_setup, allow_reexec=False)`

This diverged from `inference_sidecar.py`, which keeps re-exec enabled after runtime installation.
When runtime setup returned `runtime_action=installed_cuda_reexec`, the operator path suppressed
the process refresh required to reliably use the repaired GPU runtime.

## Fix
- Enabled runtime refresh re-exec in `compute_node_bridge.py` by removing the explicit
  `allow_reexec=False` override.
- Added regression test coverage to lock the bridge contract: runtime re-exec must remain enabled
  when CUDA runtime repair is reported.
- Added e2e-style contract tests for
  `desktop-tauri/scripts/windows_nvidia_gpu_smoke_test.py` that verify:
  - success path accepts `installed_cuda_reexec` runtime setup with CUDA-backed bridge startup
  - failure path rejects CPU fallback in bridge `started` diagnostics
- Documented the incident in structured outage JSON + this Markdown report.

## Follow-up / prevention
- Keep operator and smoke-test sidecars on a single runtime bootstrap contract.
- Preserve assertions that Windows GPU smoke validation fails closed if bridge startup reports
  CPU fallback.
- Continue surfacing `llama_module_path`, backend, offloaded layers, and KV cache diagnostics in
  startup events so regressions are detectable without manual debugger inspection.
