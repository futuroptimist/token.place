# Embedded macOS Python runtime

Apple Silicon release builds bundle a relocatable Python runtime at
`token.place desktop.app/Contents/Resources/python-runtime/bin/python3`.

- Source: `python-build-standalone` install-only CPython distribution.
- Pinned build: `cpython-3.11.10+20241016-aarch64-apple-darwin-install_only`.
- Verification: `desktop-tauri/scripts/prepare_embedded_python_runtime.py` reads
  `desktop-tauri/src-tauri/python/embedded_python_runtime_manifest.json`, downloads the exact
  immutable HTTPS asset, checks SHA-256 before extraction, validates archive layout, and writes
  safe provenance into `python-runtime/embedded-runtime-provenance.json`.
- Included packages: `desktop-tauri/src-tauri/python/requirements_desktop_runtime.txt`,
  transitive dependencies, `numpy`, `diskcache`, and Metal-capable
  `llama-cpp-python==0.3.32`.
- Update process: choose a new immutable `python-build-standalone` CPython 3.11 Apple Silicon
  asset, update the manifest URL/build/digest, run the preparation script on macOS arm64,
  validate Metal runtime probes, and keep the downloaded archive out of git and release app
  resources.

Packaged-app users do not need Python, Homebrew, Xcode, or Xcode Command Line Tools. A missing
or damaged runtime diagnostic means the app bundle should be reinstalled from a complete DMG.
