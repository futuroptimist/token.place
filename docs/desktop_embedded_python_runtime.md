# Desktop embedded Python runtime

macOS Apple Silicon release builds bundle a self-contained Python runtime at
`token.place desktop.app/Contents/Resources/python-runtime/bin/python3`.
End users installing the DMG do not need Python, Homebrew, Xcode, Xcode Command
Line Tools, CMake, or compiler toolchains.

The runtime is pinned in
`desktop-tauri/src-tauri/python/embedded_python_runtime_manifest.json` to
python-build-standalone CPython 3.11.15 build `20260623` for
`aarch64-apple-darwin`. The preparation script verifies the archive SHA-256
before extraction, installs only desktop runtime requirements plus
`llama-cpp-python==0.3.32`, requires a Metal-capable probe, and writes
`token-place-runtime-provenance.json` into the generated runtime.

To update the runtime: choose a new immutable python-build-standalone
`install_only` asset, update the manifest URL/digest/version fields, run
`desktop-tauri/scripts/prepare_embedded_python_runtime.py` on Apple Silicon, and
validate the signed `.app` with `scripts/validate_desktop_tauri_release_artifacts.py`.

If a packaged app reports `desktop_python_runtime_missing` or
`desktop_python_runtime_invalid`, the app bundle is incomplete or damaged;
reinstall token.place desktop. Do not install Python or Xcode Command Line Tools
for the packaged app. Developer builds may still set `TOKEN_PLACE_PYTHON` or
`TOKEN_PLACE_SIDECAR_PYTHON` to an explicit Python 3.11+ interpreter.
