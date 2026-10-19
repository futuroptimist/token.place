# Desktop embedded Python runtime

## Durable Windows installations (desktop 0.1.25)

The Windows installer owns its private `python-runtime` directory alongside the
installed application. Use the official NSIS/MSI installer and launch its installed
shortcut. Keep this directory together with that installation; do not relocate just
Python, share it between versions, or use a system Python or virtual environment as
a replacement. The normal per-user installation under Local AppData and installations
under Program Files persist independently of temporary staging directories.

Packaged Windows launches reject runtimes under Windows temporary directories,
including `Temp/tokenplace-step05-attempt63-*/candidate-install`. Canonical paths
are checked too, so redirecting a runtime directory into temporary storage is not
a durable installation. Staging and installer downloads may still use temporary
storage; the application and its runtime must be installed outside it before use.
Development source-tree execution retains its existing behavior.

Before starting bundled Python, the application checks essential standard-library
files (`encodings`, `json`, `os`, `platform`, and `struct`). Missing files report
`bundled_runtime_incomplete`; temporary installations report
`bundled_runtime_temporary_installation`. Other import, architecture, and runtime
provenance checks still apply. These presence checks diagnose incomplete packages;
they are not a full cryptographic attestation of all Python source files.

For either failure, reinstall the official package into a persistent location and
open its installed shortcut. Preserve existing GGUF models and reselect their paths
if needed. A missing Python standard library is not repaired by downloading a model.
The application does not automatically install dependencies, repair itself, or fall
back to host Python or a CPU runtime. GPU requirements remain unchanged.

## macOS embedded Python runtime

The Apple Silicon release DMG bundles a self-contained CPython runtime at
`token.place desktop.app/Contents/Resources/python-runtime/bin/python3`.
End users do not need Python, Homebrew, Xcode, Xcode Command Line Tools, CMake,
or compiler toolchains for packaged app startup.

Runtime source and pin:

- Source: python-build-standalone `install_only` distribution.
- CPython: 3.11.13.
- Release/build: `20250818` / `cpython-3.11.13+20250818-aarch64-apple-darwin-install_only`.
- Target: `aarch64-apple-darwin` (`arm64`).
- Checksum: verified with the SHA-256 in
  `desktop-tauri/src-tauri/python/embedded_python_runtime_manifest.json` before extraction.

The preparation script installs only the desktop runtime package set from
`requirements_desktop_runtime.txt` plus the pinned Metal-capable
`llama-cpp-python==0.3.32`, validates the Qwen 64K probe, removes build caches,
and writes `embedded_python_runtime_provenance.json` into the generated runtime.

To update the runtime, edit the manifest to a new immutable python-build-standalone
asset and SHA-256, run `desktop-tauri/scripts/prepare_embedded_python_runtime.py`
on an Apple Silicon macOS runner, verify Metal probing and release artifact
validation, then commit only the manifest/script/docs changes. Do not commit the
downloaded archive or generated `python-runtime` directory.

## Linux x86_64 CPU packages (0.1.25 release train)

Linux packaging builds a `.deb` on Ubuntu 22.04 for `x86_64-unknown-linux-gnu`.
It bundles pinned CPython 3.11.13 and the pinned desktop dependencies under the
application's `/usr/lib/<application>/python-runtime` resource directory. It does
not create a user venv or use system Python. ARM Linux, musl distributions, and
AppImage packaging are not part of this initial target.

The Linux backend is **CPU-only**. Its runtime identity is
`bundled-cpython-3.11-linux-x86_64-cpu`; GPU visibility in WSL or a VM does not make
this package CUDA-capable. Windows CUDA and Apple Silicon Metal packaging retain
their existing policies and fail-closed checks.

`prepare_linux_embedded_python_runtime.py` reuses the verified archive downloader,
safe extractor, cleanup, and provenance machinery. The Linux manifest pins the
upstream archive URL and SHA-256, architecture, and package versions. Package
validation checks every runtime file against the build inventory before running
the isolated interpreter, then checks imports, package versions, CPU backend,
and executable build identity. Startup checks the Linux standard-library layout
before launching Python and rejects missing or external core modules. A missing
runtime must be repaired by reinstalling the matching package; keep existing
GGUF model files and selections. Package integrity inventories detect accidental
damage; they are not a substitute for authenticating the downloaded package.

The Linux PR workflow retains the candidate `.deb`, checksum, and installed
structural preflight evidence for seven days. All installation in that workflow
occurs on a disposable hosted runner. Structural preflight is not real model
inference or human GUI acceptance. K370 separately gates Windows, macOS, and Linux
packaged acceptance before publication; WSL2 or VM results must identify their
environment and cannot be described as native Linux CUDA evidence.

This K372 packaging PR is explicitly assigned to the same unreleased desktop
0.1.25 train as K369. It does not authorize tagging, publishing, deployment, or
installation on an operator's computer.
