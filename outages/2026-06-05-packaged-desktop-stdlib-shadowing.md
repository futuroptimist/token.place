# 2026-06-05 packaged desktop stdlib shadowing before relay registration

## Summary

Windows 11 packaged desktop compute-node startup still failed before `Registered: yes` after the prior watchdog-timeout and subprocess-facade fixes. The latest logs showed CUDA runtime discovery and compute-plan selection succeeding, then the runtime facade child failed while importing `llama_cpp` because `llama_cpp` imported `pathlib` and Python resolved `pathlib` to a stale third-party backport in `Python311\Lib\site-packages\pathlib.py` instead of the Python 3.11 standard library.

## Impact

- Windows packaged CUDA operators could reach `Selected compute plan ... backend_selected=cuda` and `Llama init started`, but fail warm-load before `model_init.ready` and `server.registered`.
- The desktop UI stayed effectively `Running: yes / Registered: no`; no compute node appeared in the relay.
- macOS packaged `.app` paths had the same import-order risk, even though the reproduced failure was on Windows.

## Timeline / failed prior mitigations

1. **Original packaged desktop hang:** native `llama_cpp` import/model startup could hang without a bounded actionable bridge failure.
2. **Watchdog timeout fix:** import watchdogs bounded some hangs, but did not prove the packaged operator reached relay registration against a real `relay.py` lifecycle.
3. **Subprocess facade fix:** moving native runtime import/model init behind a killable subprocess surfaced early child exits, but the child still inherited an unsafe import path.
4. **New root cause:** `PYTHONNOUSERSITE=1` removed user-site paths, but did not protect against stale packages installed in the interpreter's system `site-packages`. A PyPI `pathlib.py` backport shadowed stdlib `pathlib`, then crashed on Python 3.11 with `from collections import Sequence`.

## Why CI missed it

- CI used clean Python environments and did not emulate a polluted system `site-packages` containing a stale stdlib backport.
- Earlier packaged desktop checks could pass after bridge startup/import probes without requiring `registered=true` against a real `relay.py` process.
- Mock/fake-runtime paths did not force the same child-process import ordering used by the packaged runtime facade.

## Fix / prevention

- Desktop Python path bootstrap now normalizes Windows extended-length paths for import/environment use, preserves standard-library paths before packaged app roots and `site-packages`, removes user-site paths when requested, and keeps repo-local `llama_cpp.py` shim rejection intact.
- Critical stdlib modules (`collections`, `typing`, `ctypes`, `subprocess`, `json`, `importlib`, and `pathlib`) are checked after bootstrap; shadowing reports an actionable error naming the module and bad path.
- `compute_node_bridge.py` and `model_bridge.py` avoid importing `pathlib` until after `path_bootstrap` repairs `sys.path`, so a polluted `PYTHONPATH` cannot crash startup before bootstrap.
- `utils.llm.model_manager` now applies stdlib-before-site-packages ordering to discovery/probe/runtime-worker subprocesses and guards critical stdlib resolution before importing `llama_cpp`.
- The packaged operator e2e now launches a real `relay.py`, injects an incompatible fake `site-packages/pathlib.py`, and fails unless the packaged bridge reports `registered=true` / relay runtime `ready` for both Windows-like resources and macOS `.app/Contents/Resources` layouts.

## Manual validation checklist

1. Build a new Windows desktop release from HEAD.
2. Install and launch on Windows 11 with relay URL `https://staging.token.place`, the local GGUF model, and mode `auto`.
3. Confirm logs never import `C:\Users\danie\AppData\Local\Programs\Python\Python311\Lib\site-packages\pathlib.py` for stdlib `pathlib`.
4. Confirm no `cannot import name 'Sequence' from 'collections'`, no `llama_cpp_import_timeout`, and no vague `llama_cpp_import subprocess ended` diagnostic.
5. Confirm startup reaches `Llama init completed successfully`, `model_init.ready`, `server.registered`, and UI/status `Registered: yes` with CUDA backend fields.
6. Confirm staging `/relay/diagnostics` shows one registered node with `queue_depth=0`.
7. Send a browser chat request and confirm model text returns.
8. Stop the operator and confirm the node disappears or TTL-expires without age reset; start again and confirm `Registered: yes`.
9. Repeat macOS packaged desktop validation against staging; it should either reach `Registered: yes` or fail cleanly with bounded actionable runtime diagnostics.
