# 2026-06-05 packaged desktop stdlib shadowing before relay registration

## Summary
Windows 11 packaged desktop compute-node startup still failed before relay registration after the
previous watchdog and subprocess-facade fixes. Runtime setup selected CUDA and `llama_cpp` discovery
completed, but the subprocess runtime facade later imported a stale PyPI `pathlib.py` backport from
system `site-packages` instead of Python 3.11's stdlib `pathlib`.

## Symptoms
- Tauri launched packaged `model_bridge.py` and `compute_node_bridge.py` successfully.
- `desktop.runtime_setup` selected CUDA and found the user's CUDA-enabled `llama_cpp` package.
- Model warm-load reached compute-plan selection with `backend_selected=cuda` and `n_gpu_layers=-1`.
- The runtime facade child then failed while importing `llama_cpp` because `llama_cpp` imported
  `pathlib`, and `pathlib` resolved to `...\Lib\site-packages\pathlib.py`.
- That stale backport executed `from collections import Sequence`, which is invalid on modern
  Python, causing `ImportError: cannot import name 'Sequence' from 'collections'`.
- Warm-load failed, the desktop never emitted `server.registered`, and the UI stayed short of
  `Registered: yes`.

## Timeline
- Original packaged desktop hang: warm-load stopped progressing while locating or importing
  `llama_cpp`, so registration never happened.
- Failed watchdog timeout fix: separate child import watchdogs made the hang bounded but introduced
  `llama_cpp_import_timeout` from an import environment that could diverge from the real bridge.
- Failed subprocess facade fix: parent startup reused the desktop probe and selected CUDA, but the
  child facade still received an unsafe `sys.path` order and hid the real import cause until stdout
  and stderr tails were added.
- New root cause: the successful probe path was later prepended ahead of stdlib, letting a stale
  `site-packages/pathlib.py` shadow stdlib during the facade child's `llama_cpp` import.

## Root cause
Packaged runtime import isolation was incomplete. `PYTHONNOUSERSITE=1` removed user-site packages,
but it did not protect Python's standard library from stale packages installed in the interpreter's
system `site-packages`. Model-manager probe reuse inserted the probed `llama_cpp` package parent at
the front of `sys.path`, and subprocess bootstrap code propagated that ordering to child workers.
That made third-party backports eligible before stdlib modules such as `pathlib`.

## Why CI/e2e missed it
Existing packaged tests exercised mock registration and facade early-exit diagnostics, but they did
not simulate a polluted system `site-packages` containing an incompatible stdlib backport while also
requiring a real `relay.py` lifecycle to reach `registered=true`. Mock runtime flows could pass with
`Running: yes` without proving that the runtime-backed packaged operator reached `Registered: yes`
against a real relay process.

## Corrective actions in this PR
- Keep stdlib import entries ahead of `site-packages`/`dist-packages` when packaged bootstrap,
  runtime probes, and runtime facade children mutate `sys.path`.
- Insert a probed `llama_cpp` package parent after stdlib instead of at the absolute front.
- Add stdlib shadowing diagnostics for critical modules, including `pathlib`, with actionable
  messages such as `stdlib module pathlib shadowed by ...site-packages/pathlib.py`.
- Stop passing ambient `PYTHONPATH` into desktop runtime setup probes, because Python evaluates it
  before stdlib during interpreter startup and before `path_bootstrap` can repair ordering.
- Preserve packaged import roots, Windows paths with spaces, `\\?\` prefix normalization, macOS
  `.app/Contents/Resources` roots, legitimate third-party dependencies, and repo-local
  `llama_cpp.py` shim rejection.
- Extend packaged e2e coverage so standard Windows-like resources and macOS `Contents/Resources`
  layouts launch a real `relay.py`, use a fake runtime that imports `pathlib`, include a polluted
  `site-packages/pathlib.py`, and fail unless the bridge emits `registered=true` with
  `relay_runtime_state=ready`.

## Manual Windows CUDA validation checklist
1. Build a new Windows desktop release from this commit.
2. Install and launch it on Windows 11.
3. Start the operator against `https://staging.token.place` with the CUDA GGUF model and mode `auto`.
4. Confirm logs do not import `C:\Users\danie\AppData\Local\Programs\Python\Python311\Lib\site-packages\pathlib.py`.
5. Confirm no `cannot import name 'Sequence' from 'collections'`, no `llama_cpp_import_timeout`, and
   no `llama_cpp_import subprocess ended`.
6. Confirm logs show CUDA compute-plan selection, `Llama init completed successfully`,
   `model_init.ready`, `server.registered`, and UI `Registered: yes`.
7. Confirm staging `/relay/diagnostics` shows one registered node with `queue_depth=0`.
8. Send a browser chat message and confirm model text returns.
9. Stop the operator and confirm unregister/TTL cleanup; start again and confirm `Registered: yes`.
10. Run the macOS packaged operator against staging and confirm it reaches `Registered: yes` or fails
    with bounded actionable runtime diagnostics.
