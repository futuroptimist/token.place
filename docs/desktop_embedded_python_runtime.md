# macOS embedded Python runtime

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
