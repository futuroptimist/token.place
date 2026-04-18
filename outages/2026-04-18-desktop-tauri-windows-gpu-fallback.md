# Outage: desktop-tauri Windows GPU mode fell back to CPU during runtime bootstrap

- **Date:** 2026-04-18
- **Slug:** `desktop-tauri-windows-gpu-fallback`
- **Affected area:** desktop-tauri Windows 11 + NVIDIA/CUDA runtime bootstrap

## Summary
On Windows desktop installs, startup could report a successful runtime setup while effectively
running `llama-cpp-python` in CPU mode.

## Impact
Operators requesting `auto`, `gpu`, or `hybrid` mode on NVIDIA-capable Windows hosts could still end
up in CPU execution, with reduced inference performance and ambiguous diagnostics.

## Root cause
1. Windows CUDA wheel fallback targeted a single CUDA wheel index first, then used an unpinned
   fallback that included PyPI as an extra index.
2. That unpinned fallback could resolve to a CPU wheel from PyPI when matching CUDA wheels were not
   available for the active Python ABI/channel.
3. The runtime could therefore proceed with CPU runtime availability after CUDA installation attempts
   without deterministically exhausting CUDA-only channels first.

## Remediation
- Expanded Windows CUDA fallback plans to walk multiple CUDA wheel channels (`cu128`, `cu126`,
  `cu125`, `cu124`) before CPU fallback.
- Updated unpinned CUDA fallback plans to avoid PyPI extra-index fallback for the
  `llama-cpp-python` package candidate.
- Mirrored hardware-acceleration requirements in `AGENTS.md` so future work keeps bootstrap logic
  aligned with README guidance.
- Added unit and e2e regression tests to validate fallback ordering and successful CUDA activation
  when a later CUDA channel becomes available.

## Follow-up / prevention
- Keep Windows CUDA channel coverage updated when upstream wheel publishing patterns change.
- Preserve structured backend diagnostics (`backend_available`, `backend_used`, fallback reason)
  through desktop bridge startup events.
- Treat CPU fallback as a last-resort recovery path, never as a silent substitute for GPU mode.
