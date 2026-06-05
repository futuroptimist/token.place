# 2026-06-05 packaged desktop stdlib shadowing blocked Windows registration

## Summary
Windows packaged desktop compute-node startup could still fail before relay registration after the
watchdog and subprocess-facade fixes. Runtime setup found the user's Python 3.11 CUDA
`llama-cpp-python` install and the model manager selected CUDA, but the facade child imported a stale
third-party `pathlib.py` backport from `site-packages` instead of Python 3.11's stdlib `pathlib`.

## Symptoms
- Packaged Tauri launched `model_bridge.py` and `compute_node_bridge.py` successfully, including paths
  with spaces such as `token.place desktop`.
- `desktop_runtime_setup` reported CUDA support and a valid `llama_cpp` module path.
- Warm-load progressed through runtime discovery, CUDA compute-plan selection, and `Llama init started`.
- The subprocess runtime facade then failed while importing `llama_cpp` because `llama_cpp` imported
  `pathlib`, and Python resolved `pathlib` to `...\Lib\site-packages\pathlib.py`.
- That stale PyPI backport executed `from collections import Sequence`, which fails on modern Python,
  so the bridge never emitted `server.registered` and the UI never reached `Registered: yes`.

## Root cause
The packaged runtime/facade import path still allowed third-party `site-packages` entries to appear
before stdlib roots in subprocess import state. `PYTHONNOUSERSITE=1` only removes user-site packages;
it does not protect stdlib modules from stale packages installed in the interpreter's system
`site-packages`. The facade also inherited explicit probe/runtime paths that could put
`site-packages` ahead of stdlib while trying to preserve access to legitimate dependencies such as
`llama_cpp`, `requests`, `cryptography`, and `psutil`.

## Why prior fixes missed it
- The original watchdog timeout fix proved that a child import could be bounded, but it did not keep
  stdlib roots ahead of third-party paths.
- The subprocess facade fix made child failure diagnostics actionable, but the child still had an
  import-order contract that could prefer stale backports over Python 3.11 stdlib modules.
- Existing CI exercised clean Linux/macOS-style environments and mock runtime paths. It did not
  emulate a polluted system `site-packages` containing an incompatible `pathlib.py`, and the earlier
  packaged e2e could pass if startup reached a running state without proving `registered=true` on the
  real relay lifecycle.

## Corrective actions in this PR
- Harden desktop bridge path bootstrap so packaged import roots remain available while stdlib roots
  stay ahead of `site-packages` / `dist-packages` entries.
- Normalize `\\?\` Windows extended-length prefixes before placing paths in `sys.path` or subprocess
  environment variables.
- Add guarded stdlib diagnostics for critical modules including `pathlib`; if a guarded stdlib module
  still resolves from third-party site-packages, startup fails with an actionable message naming the
  module and bad path.
- Apply the same stdlib-before-site ordering to `llama_cpp` discovery probes and subprocess runtime
  workers while preserving repo-local `llama_cpp.py` shim rejection.
- Extend packaged e2e coverage for standard Windows-like resources and macOS
  `.app/Contents/Resources` layouts to inject a stale `pathlib.py` pollution path and still require
  `registered=true` / ready status against a real `relay.py` process.

## Manual validation checklist
1. Build a Windows desktop release from HEAD and launch it on the Windows 11 CUDA machine.
2. Start the operator against `https://staging.token.place` with the local GGUF model and mode `auto`.
3. Confirm logs show CUDA selection, `Llama init completed successfully`, `model_init.ready`,
   `server.registered`, and UI `Registered: yes`.
4. Confirm logs do not import `...\Lib\site-packages\pathlib.py` and do not contain
   `cannot import name 'Sequence' from 'collections'`.
5. Confirm staging `/relay/diagnostics` shows one registered node with `queue_depth=0`.
6. Send a browser chat message and confirm model text returns.
7. Stop and restart the operator and confirm registration returns without an unregister attempt before
   any successful registration.
8. Repeat macOS packaged desktop validation against staging; it should reach `Registered: yes` or fail
   cleanly with bounded actionable runtime diagnostics.
